# coding=utf-8
__author__ = "Dimitrios Karkalousos"

from abc import ABC

import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from torch.nn import L1Loss

from mridc.collections.common.losses.ssim import SSIMLoss
from mridc.collections.common.parts.fft import ifft2c
from mridc.collections.common.parts.utils import coil_combination
from mridc.collections.reconstruction.models.base import BaseMRIReconstructionModel, BaseSensitivityModel
from mridc.collections.reconstruction.models.cascadenet.ccnn_block import CascadeNetBlock
from mridc.collections.reconstruction.models.conv.conv2d import Conv2d
from mridc.collections.reconstruction.parts.utils import center_crop_to_smallest
from mridc.core.classes.common import typecheck

__all__ = ["CascadeNet"]


class CascadeNet(BaseMRIReconstructionModel, ABC):
    """
    Implementation of the Deep Cascade of Convolutional Neural Networks, as presented in Schlemper, J., \
    Caballero, J., Hajnal, J. V., Price, A., & Rueckert, D.

    References
    ----------

    ..

        Schlemper, J., Caballero, J., Hajnal, J. V., Price, A., & Rueckert, D., A Deep Cascade of Convolutional \
        Neural Networks for MR Image Reconstruction. Information Processing in Medical Imaging (IPMI), 2017. \
        Available at: https://arxiv.org/pdf/1703.00555.pdf

    """

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        # init superclass
        super().__init__(cfg=cfg, trainer=trainer)

        cfg_dict = OmegaConf.to_container(cfg, resolve=True)

        self.fft_type = cfg_dict.get("fft_type")

        # Cascades of CascadeCNN blocks
        self.cascades = torch.nn.ModuleList(
            [
                CascadeNetBlock(
                    Conv2d(
                        in_channels=2,
                        out_channels=2,
                        hidden_channels=cfg_dict.get("hidden_channels"),
                        n_convs=cfg_dict.get("n_convs"),
                        batchnorm=cfg_dict.get("batchnorm"),
                    ),
                    fft_type=self.fft_type,
                    no_dc=cfg_dict.get("no_dc"),
                )
                for _ in range(cfg_dict.get("num_cascades"))
            ]
        )

        self.output_type = cfg_dict.get("output_type")

        # Initialize the sensitivity network if use_sens_net is True
        self.use_sens_net = cfg_dict.get("use_sens_net")
        if self.use_sens_net:
            self.sens_net = BaseSensitivityModel(
                cfg_dict.get("sens_chans"),
                cfg_dict.get("sens_pools"),
                fft_type=self.fft_type,
                mask_type=cfg_dict.get("sens_mask_type"),
                normalize=cfg_dict.get("sens_normalize"),
            )

        # initialize weights if not using pretrained ccnn
        # TODO if not cfg_dict.get("pretrained", False)
        self.train_loss_fn = SSIMLoss() if cfg_dict.get("train_loss_fn") == "ssim" else L1Loss()
        self.eval_loss_fn = SSIMLoss() if cfg_dict.get("eval_loss_fn") == "ssim" else L1Loss()

        self.accumulate_estimates = False
        self.dc_weight = torch.nn.Parameter(torch.ones(1))

    @typecheck()
    def forward(
        self,
        y: torch.Tensor,
        sensitivity_maps: torch.Tensor,
        mask: torch.Tensor,
        init_pred: torch.Tensor,
        target: torch.Tensor,
    ) -> torch.Tensor:
        """
        Forward pass of the network.

        Parameters
        ----------
        y: Subsampled k-space data.
            torch.Tensor, shape [batch_size, n_coils, n_x, n_y, 2]
        sensitivity_maps: Coil sensitivity maps.
            torch.Tensor, shape [batch_size, n_coils, n_x, n_y, 2]
        mask: Sampling mask.
            torch.Tensor, shape [1, 1, n_x, n_y, 1]
        init_pred: Initial prediction.
            torch.Tensor, shape [batch_size, n_x, n_y, 2]
        target: Target data to compute the loss.
            torch.Tensor, shape [batch_size, n_x, n_y, 2]

        Returns
        -------
        pred: list of torch.Tensor, shape [batch_size, n_x, n_y, 2], or  torch.Tensor, shape [batch_size, n_x, n_y, 2]
             If self.accumulate_loss is True, returns a list of all intermediate estimates.
             If False, returns the final estimate.
        """
        sensitivity_maps = self.sens_net(y, mask) if self.use_sens_net else sensitivity_maps
        pred = y.clone()
        for cascade in self.cascades:
            pred = cascade(pred, y, sensitivity_maps, mask)
        pred = torch.view_as_complex(
            coil_combination(ifft2c(pred, fft_type=self.fft_type), sensitivity_maps, method=self.output_type, dim=1)
        )
        _, pred = center_crop_to_smallest(target, pred)
        return pred
