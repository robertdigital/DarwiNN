#  Copyright (c) 2019, Xilinx
#  All rights reserved.
#  
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#  
#  1. Redistributions of source code must retain the above copyright notice, this
#     list of conditions and the following disclaimer.
#  
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#  
#  3. Neither the name of the copyright holder nor the names of its
#     contributors may be used to endorse or promote products derived from
#     this software without specific prior written permission.
#  
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#  FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#  DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#  SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import torch
import torch.distributed as t_d
import torch.multiprocessing as mp
import copy
import numpy as np
import math
from darwinn.utils.fitness import compute_centered_ranks
from darwinn.utils.fitness import compute_normalized_ranks
from darwinn.utils.noise import *

class DarwiNNOptimizer(object):
    """Abstract class for optimizer functions"""
    def __init__(self, environment, model, criterion, popsize=100, data_parallel=False):
        #Disable Autograd
        torch.autograd.set_grad_enabled(False)
        #set environment
        self.environment = environment
        #set number of population-parallel working nodes
        self.data_parallel = data_parallel
        if self.data_parallel:
            self.nodes = 1
        else:
            self.nodes = self.environment.number_nodes
        #round down requested population to something divisible by number of ranks
        self.popsize = (popsize // self.nodes) * self.nodes
        #evenly divide population between ranks
        self.folds = self.popsize // self.nodes
        print("NE Optimizer parameters: population=",self.popsize,", folds=",self.folds)
        #define data structures to hold fitness values
        self.fitness_global = torch.empty((self.popsize,), device=self.environment.device)
        self.fitness_list = list(torch.chunk(self.fitness_global,self.nodes,dim=0))
        self.fitness_local = self.fitness_list[self.environment.rank if self.nodes != 1 else 0]
        #initialize model and theta
        self.model_adapt = model
        self.model = copy.deepcopy(model)
        if self.environment.cuda:
            self.model.cuda()
            self.model_adapt.cuda()
        self.num_parameters = self.count_num_parameters()
        self.criterion = criterion
        self.theta = torch.empty((self.num_parameters), device=self.environment.device)
        self.update_theta()
        self.loss = 0
        #fitness synchronization depends on parallelism mode
        if self.data_parallel:
            self.fitness_sync_mode = "AVERAGE"
        else:
            self.fitness_sync_mode = "GATHER"
        #initialize generation
        self.generation = 1
    
    #performs one generation of the evolution process
    def step(self, data, target):
        self.mutate()
        self.eval_fitness(data, target)
        self.environment.synchronize(self.fitness_local,mode=self.fitness_sync_mode,lst=self.fitness_list)
        self.select()
        self.adapt()
        self.generation += 1
    
    #select by fitness and prepare for next generation
    def select(self):
        raise NotImplementedError
    
    #select by fitness and prepare for next generation
    def adapt(self):
        raise NotImplementedError
    
    #transforms a model for the next generation
    def mutate(self):
        raise NotImplementedError
    
    def eval_fitness(self, data, target):
        raise NotImplementedError
    
    def eval_theta(self, data, target):
        raise NotImplementedError
    
    def get_loss(self):
        raise NotImplementedError

    def count_num_parameters(self):
        orig_params = []
        for param in self.model.parameters():
            p = param.data.cpu().numpy()
            orig_params.append(p.flatten())
        orig_params_flat = np.concatenate(orig_params)
        return len(orig_params_flat)

    """Updates the NN model from the value of Theta"""
    def update_model(self, theta):
        idx = 0
        for param in self.model.parameters():
            flattened_dim = param.numel()
            temp = theta[idx:idx+flattened_dim]
            temp = temp.view_as(param)
            param.data = temp.data
            idx += flattened_dim

    """Updates the NN model gradients"""
    def update_grad(self, grad):
        idx = 0
        for param in self.model_adapt.parameters():
            flattened_dim = param.numel()
            temp = grad[idx:idx+flattened_dim]
            temp = temp.view_as(param.data)
            param.grad = temp.data
            idx += flattened_dim

    """Updates Theta from the NN model"""
    def update_theta(self):
        idx = 0
        for param in self.model_adapt.parameters():
            flattened_dim = param.numel()
            self.theta[idx:idx+flattened_dim] = param.data.flatten()
            idx += flattened_dim
            
    def eval_theta(self, data, target):
        output = self.model_adapt(data)
        self.loss = self.criterion(output, target).item()
        return output
        
    def get_loss(self):
        return self.loss
    
class OpenAIESOptimizer(DarwiNNOptimizer):
    """Implements Open-AI ES optimizer"""
    def __init__(self, environment, model, criterion, optimizer, distribution="Gaussian", sampling="Antithetic", sigma=0.1, popsize=100, data_parallel=False, semi_updates=False, orthogonal_updates=False):
        super(OpenAIESOptimizer,self).__init__(environment, model, criterion, popsize, data_parallel)
        self.optimizer = optimizer
        self.distribution = distribution
        self.sampling = sampling
        self.sigma = sigma
        #configure theta updates
        if data_parallel and (orthogonal_updates or semi_updates):
            raise Exception("Semi- or Orthogonal Theta updates cannot be performed in data-parallel mode")
        self.semi_updates = semi_updates
        self.orthogonal_updates = orthogonal_updates
        #define gradient data structure(s)
        if self.orthogonal_updates:
            gradient_chunk_size = math.ceil(self.num_parameters/self.nodes)
            gradients_len = gradient_chunk_size*self.nodes #round up gradient size to evenly divide into number of nodes
        else:
            gradients_len = self.num_parameters
        self.gradient = torch.empty((gradients_len), device=self.environment.device)
        self.gradient_list = list(torch.chunk(self.gradient,self.nodes,dim=0))
        self.gradient_local = self.gradient_list[self.environment.rank if self.nodes != 1 else 0]
        #configure theta updates
        if data_parallel and (orthogonal_updates or semi_updates):
            raise Exception("Semi- or Orthogonal Theta updates cannot be performed in data-parallel mode")
        self.semi_updates = semi_updates
        self.orthogonal_updates = orthogonal_updates
        if self.semi_updates:
            self.fitness_sync_mode = "NONE" #prevent fitness synchronization, not required in this mode
            self.gradient_sync_mode = "AVERAGE"
            self.fitness_for_update = self.fitness_local
            self.update_noise_mode = NoiseMode.SLICE_H
            self.gradient_for_update = self.gradient
            self.gradient_for_sync = self.gradient
        elif self.orthogonal_updates:
            self.gradient_sync_mode = "GATHER"
            self.fitness_for_update = self.fitness_global
            self.update_noise_mode = NoiseMode.SLICE_V
            self.gradient_for_sync = self.gradient_local
            self.gradient_for_update = self.gradient_local[:min(gradient_chunk_size,gradients_len-self.environment.rank*gradient_chunk_size)]
        else:
            self.gradient_sync_mode = "NONE"
            self.fitness_for_update = self.fitness_global
            self.update_noise_mode = NoiseMode.FULL
            self.gradient_for_update = self.gradient
            self.gradient_for_sync = self.gradient
        #initialize noise generator
        if self.data_parallel:
            self.mutate_noise_mode = NoiseMode.FULL #for DDP, mutate all population
        else:
            self.mutate_noise_mode = NoiseMode.SLICE_H #for DPP, mutate just population assigned to local node
        self.epsilon = NoiseGenerator(self.popsize, self.num_parameters, self.environment.device, self.environment.number_nodes, self.environment.rank, distribution=self.distribution, sampling=self.sampling, mutate_mode=self.mutate_noise_mode, update_mode=self.update_noise_mode)
        #temporary variables
        self.theta_noisy = None
        self.fitness_shaped = None
        #define random distribution
        if (self.distribution == "Gaussian"):
            self.randfunc = torch.randn
        elif (self.distribution == "Uniform"):
            self.randfunc = torch.rand
        else:
            raise ValueError

    def select(self):
        self.fitness_shaped = compute_centered_ranks(-self.fitness_for_update, device=self.environment.device)

    def adapt(self):
        #compute gradient (with optional synchronization)
        torch.mv(self.epsilon.generate_update_noise().t(), self.fitness_shaped, out=self.gradient_for_update)
        #synchronize gradient
        self.environment.synchronize(self.gradient_for_sync, mode=self.gradient_sync_mode, lst=self.gradient_list)
        #normalize gradient
        self.gradient /= self.sigma * self.popsize
        #use gradients to update model and then get new theta
        self.update_grad(-self.gradient)
        self.optimizer.step()
        self.update_theta()
    
    def mutate(self):
        self.epsilon.step()
        self.theta_noisy = self.theta + self.epsilon.generate_mutate_noise()*self.sigma
    
    def eval_fitness(self, data, target):
        for i in range(self.folds):
            self.update_model(self.theta_noisy[i])
            output = self.model(data)
            self.fitness_local[i] = self.criterion(output, target).item()
        self.loss = torch.mean(self.fitness_local).item()

class GAOptimizer(DarwiNNOptimizer):
    """Implements a simple Genetic Algorithm optimizer"""
    def __init__(self, environment, model, criterion, sigma=0.1, popsize=100, elite_ratio=0.1, mutation_probability=0.01, data_parallel=False):
        super(GAOptimizer,self).__init__(environment, model, criterion, popsize, data_parallel)
        self.fold_offset = self.environment.rank*self.folds
        self.num_elites = int(self.popsize*elite_ratio)
        self.elites = torch.zeros((self.num_elites,self.num_parameters), device=self.environment.device)
        self.population = torch.zeros((self.popsize,self.num_parameters), device=self.environment.device)
        self.sigma = sigma

    def select(self):
        #sort by fitness
        fitness_sorted, ind = self.fitness_global.sort()
        #select elites from population using indices of top fitnesses
        self.elites = torch.index_select(self.population,0,ind[:self.num_elites])   
                
    def mutate(self):
        #elites are inherited
        for i in range(self.num_elites):
            self.population[i] = self.elites[i]
        #rest of population is generated
        for i in range(self.num_elites,self.popsize):
            idx1 = torch.randint(0,self.num_elites,(1,),device=self.environment.device)
            idx2 = torch.randint(0,self.num_elites,(1,),device=self.environment.device)
            parent_1 = self.elites[idx1.item()]
            parent_2 = self.elites[idx2.item()]
            parent1_select = torch.randint(0,2,(self.num_parameters,), dtype=torch.float, device=self.environment.device)
            parent2_select = (parent1_select - 1.0) * -1.0
            #crossover
            self.population[i] = self.elites[idx1.item()] * parent1_select
            self.population[i] += parent_2 * parent2_select
            #mutation
            self.population[i] += torch.randn((self.num_parameters,), device=self.environment.device)*self.sigma

    def adapt(self):
        pass

    def eval_fitness(self, data, target):
        for i in range(self.folds):
            self.update_model(self.population[self.fold_offset+i])
            output = self.model(data)
            self.fitness_local[i] = self.criterion(output, target).item()
        self.loss = torch.mean(self.fitness_local).item()

class SNESOptimizer(OpenAIESOptimizer):
    """Implements Open-AI ES optimizer"""
    def __init__(self, environment, model, criterion, optimizer, distribution="Gaussian", sampling="Antithetic", sigma=0.1, popsize=100, data_parallel=False, semi_updates=False, orthogonal_updates=False):
        print(distribution)
        super().__init__(environment, model, criterion, optimizer, distribution, sampling, sigma, popsize, data_parallel, semi_updates, orthogonal_updates)
        self.sigma = torch.empty((self.num_parameters), device=self.environment.device)
        self.sigma.fill_(sigma)
        self.lr_theta = torch.tensor(0.001, device=self.environment.device)
        self.lr_sigma = torch.tensor((3+torch.log(torch.tensor(self.num_parameters, dtype=torch.float))) / (5 * torch.sqrt(torch.tensor(self.num_parameters,dtype=torch.float))), device = self.environment.device)
        self.theta = torch.zeros((self.num_parameters), device=self.environment.device)

    def select(self):
        #compute utility based on arXiv:1906.03139
        self.fitness_shaped = compute_normalized_ranks(self.fitness_for_update, r=0.5, device=self.environment.device)
        self.utilities = self.fitness_shaped.view(len(self.fitness_shaped), 1)

    def adapt(self):
        #calculate theta gradient based on 10.1145/2001576.2001692
        self.theta_gradient = torch.mm(self.epsilon.generate_update_noise().t(), self.utilities)
        self.theta_gradient = self.theta_gradient.view(self.num_parameters)
        #calculate sigma gradient based on 10.1145/2001576.2001692
        self.sigma_gradient = torch.mm(( (self.epsilon.generate_update_noise()*self.epsilon.generate_update_noise()) - 1).t(), self.utilities)
        self.sigma_gradient = self.sigma_gradient.view(self.num_parameters)
        self.update_dist()
        self.update_model(self.theta)
    
    def update_dist(self):
        #update theta and sigma based on 10.1145/2001576.2001692
        self.theta_gradient /= self.popsize
        self.theta_gradient /= self.sigma
        self.theta += self.lr_theta * self.theta_gradient
        self.exponent = 0.5 * self.lr_sigma * self.sigma_gradient
        self.sigma *= torch.exp(self.exponent) 
    
    def eval_theta(self, data, target):
        self.update_model(self.theta)
        output = self.model(data)
        self.loss = self.criterion(output, target).item()
        return output
