import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np

from hflayers import HopfieldLayer

from DiscretisedLogisticMixture import DiscretisedLogisticMixture

class Residual(nn.Module):
    def __init__(self, in_channels, num_hiddens, num_residual_hiddens):
        super(Residual, self).__init__()
        self._block = nn.Sequential(
            nn.ReLU(True),
            nn.Conv2d(in_channels=in_channels,
                      out_channels=num_residual_hiddens,
                      kernel_size=3, stride=1, padding=1, bias=False),
            nn.ReLU(True),
            nn.Conv2d(in_channels=num_residual_hiddens,
                      out_channels=num_hiddens,
                      kernel_size=1, stride=1, bias=False)
        )
    
    def forward(self, x):
        return x + self._block(x)


class ResidualStack(nn.Module):
    def __init__(self, in_channels, num_hiddens, num_residual_layers, num_residual_hiddens):
        super(ResidualStack, self).__init__()
        self._num_residual_layers = num_residual_layers
        self._layers = nn.ModuleList([Residual(in_channels, num_hiddens, num_residual_hiddens)
                             for _ in range(self._num_residual_layers)])

    def forward(self, x):
        for i in range(self._num_residual_layers):
            x = self._layers[i](x)
        return F.relu(x)


class Encoder(nn.Module):
    def __init__(self, in_channels, num_hiddens, num_residual_layers, num_residual_hiddens):
        super(Encoder, self).__init__()

        self.conv_1 = nn.Conv2d(in_channels=in_channels,
                                 out_channels=num_hiddens//2,
                                 kernel_size=4,
                                 stride=2, padding=1)

        self.conv_2 = nn.Conv2d(in_channels=num_hiddens//2,
                                 out_channels=num_hiddens,
                                 kernel_size=4,
                                 stride=2, padding=1)

        self.conv_3 = nn.Conv2d(in_channels=num_hiddens,
                                 out_channels=num_hiddens,
                                 kernel_size=4,
                                 stride=1, padding=2)

        self.conv_4 = nn.Conv2d(in_channels=num_hiddens,
                                 out_channels=num_hiddens,
                                 kernel_size=3,
                                 stride=1, padding=1)

        self.residual_stack = ResidualStack(in_channels=num_hiddens,
                                             num_hiddens=num_hiddens,
                                             num_residual_layers=num_residual_layers,
                                             num_residual_hiddens=num_residual_hiddens)

    def forward(self, inputs):
        x = self.conv_1(inputs)
        x = F.relu(x)
        
        x = self.conv_2(x)
        x = F.relu(x)
        
        x = self.conv_3(x)
        x = F.relu(x)

        x = self.conv_4(x)
        #Should have 2048 units -> embedding_dim * repres_dim^2
        return self.residual_stack(x)


class Decoder(nn.Module):
    def __init__(self, in_channels, out_channels, num_hiddens, num_residual_layers, num_residual_hiddens):
        super(Decoder, self).__init__()
        
        self.conv_1 = nn.Conv2d(in_channels=in_channels,
                                 out_channels=num_hiddens,
                                 kernel_size=3, 
                                 stride=1, padding=1)
        
        self.residual_stack = ResidualStack(in_channels=num_hiddens,
                                             num_hiddens=num_hiddens,
                                             num_residual_layers=num_residual_layers,
                                             num_residual_hiddens=num_residual_hiddens)
        
        self.conv_trans_1 = nn.ConvTranspose2d(in_channels=num_hiddens, 
                                                out_channels=num_hiddens//2,
                                                kernel_size=4, 
                                                stride=1, padding=2)

        self.conv_trans_2 = nn.ConvTranspose2d(in_channels=num_hiddens//2, 
                                                out_channels=num_hiddens//2,
                                                kernel_size=4, 
                                                stride=2, padding=1)

        self.conv_trans_3 = nn.ConvTranspose2d(in_channels=num_hiddens//2, 
                                                out_channels=out_channels,
                                                kernel_size=4, 
                                                stride=2, padding=1)

    def forward(self, inputs):
        x = self.conv_1(inputs)
        
        x = self.residual_stack(x)
        
        x = self.conv_trans_1(x)
        x = F.relu(x)

        x = self.conv_trans_2(x)
        x = F.relu(x)
        
        return self.conv_trans_3(x)

class HopVAE(nn.Module):
    def __init__(self, config, device):
        super(HopVAE, self).__init__()

        self.device = device

        self.num_embeddings = config.num_embeddings
        self.embedding_dim = config.embedding_dim
        self.representation_dim = config.representation_dim
        self.image_size = config.image_size
        self.num_levels = config.num_levels
        self.num_channels = config.num_channels
        self.num_mixtures = config.num_mixtures

        self.encoder = Encoder(config.num_channels, config.num_hiddens,
                                config.num_residual_layers, 
                                config.num_residual_hiddens)

        self.pre_vq_conv = nn.Conv2d(in_channels=config.num_hiddens, 
                                      out_channels=config.embedding_dim,
                                      kernel_size=1, 
                                      stride=1)

        self.hopfield = HopfieldLayer(
                            input_size=config.embedding_dim,                           # R
                            quantity=config.num_embeddings,                             # W_K
                            stored_pattern_as_static=True,
                            state_pattern_as_static=True
                        )

        self.decoder = Decoder(config.embedding_dim,
                        self.num_mixtures * (1 + self.num_channels * 2),
                        config.num_hiddens, 
                        config.num_residual_layers, 
                        config.num_residual_hiddens)

        self.discretised_logistic_mixture = DiscretisedLogisticMixture(config, device)

    def forward(self, X):
        Z = self.encoder(X)
        Z = self.pre_vq_conv(Z)

        Z = Z.permute(0, 2, 3, 1).contiguous()
        Z = Z.view(-1, self.representation_dim * self.representation_dim, self.embedding_dim)

        Z_embeddings = self.hopfield(Z)

        Z_embeddings = Z_embeddings.view(-1, self.representation_dim, self.representation_dim, self.embedding_dim)
        Z_embeddings = Z_embeddings.permute(0, 3, 1, 2).contiguous()

        dist_params = self.decoder(Z_embeddings).view(-1, self.num_channels * (self.image_size ** 2), self.num_mixtures * (1 + self.num_channels * 2))

        log_scale_min = -32.23619130191664
        #dist_params = dist_params.permute(0, 2, 1)
        #dist_params = dist_params.transpose(1, 2)

        # unpack parameters. (B, T, num_mixtures) x 3
        logit_PI = dist_params[:, :, :self.num_mixtures]
        MU = dist_params[:, :, self.num_mixtures:2 * self.num_mixtures]
        log_S = torch.clamp(dist_params[:, :, 2 * self.num_mixtures:3 * self.num_mixtures], min=log_scale_min)

        if self.training:
            X = X.view(-1, (self.num_channels * self.image_size ** 2), 1)
            probs = self.discretised_logistic_mixture(logit_PI, MU, log_S, X)
            return probs
        else:
            sample = self.discretised_logistic_mixture.sample(logit_PI, MU, log_S)
            return sample