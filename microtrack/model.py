"""

This module is used to construct and solve models of diffusion data 

"""
import numpy as np
# Import stuff for sparse matrices:
import scipy.sparse as sps
import dipy.reconst.dti as dti

import microtrack.descriptors as desc
import microtrack.fibers as mtf
import microtrack.tensor as mtt
import microtrack.dwi as dwi
import microtrack.utils as mtu


# Global constants:
AD = 1.5
RD = 0.5
# This converts b values from , so that it matches the units of ADC we use in
# the Stejskal/Tanner equation: 
SCALE_FACTOR = 1000

class BaseModel(desc.ResetMixin):
    """
    Base-class for models.
    """
    def __init__(self,DWI,scaling_factor=SCALE_FACTOR):
        """
        
        """
        # If you provided file-names and not a DWI class object, we will
        # generate one for you right here and replace it inplace: 
        if DWI.__class__ in [list, np.ndarray, tuple]:
            DWI = dwi.DWI(DWI[0], DWI[1], DWI[2])
        
        self.data = DWI.data
        self.bvecs = DWI.bvecs
        
        # This factor makes sure that we have the right units for the way we
        # calculate the ADC: 
        self.bvals = DWI.bvals/scaling_factor

        # Get the inverse of the DWI affine, which xforms from fiber
        # coordinates (which are in xyz) to image coordinates (which are in ijk):
        self.affine = DWI.affine.getI()
        
    @desc.auto_attr
    def b_idx(self):
        """
        The indices into non-zero b values
        """
        return np.where(self.bvals > 0)[0]
        
    @desc.auto_attr
    def b0_idx(self):
        """
        The indices into zero b values
        """
        return np.where(self.bvals==0)[0]

    @desc.auto_attr
    def S0(self):
        """
        Extract and average the signal for volumes in which no b weighting was
        used (b0 scans)
        """
        return np.mean(self.data[...,self.b0_idx],-1).squeeze()
        
    @desc.auto_attr
    def S_weighted(self):
        """
        The signal in b-weighted volumes
        """
        return self.data[...,self.b_idx].squeeze()

    def fit(self):
        """
        The pattern is that each one of the models will have a fit method that
        goes and fits the particulars of that model.

        XXX Need to consider the commonalities which can be outlined here. 
        """
        pass
    
class TensorModel(BaseModel):
    """
    A class for representing and solving a simple forward model. Just the
    diffusion tensor.
    
    """
    def __init__(self,
                 DWI,
                 scaling_factor=SCALE_FACTOR,
                 mask=None):
        """
        Parameters
        -----------
        DWI: A microtrack.dwi.DWI class instance, or a list containing: [the
        name of nifti file, from which data should be read, bvecs file, bvals
        file]

        scaling_factor: This scales the b value for the Stejskal/Tanner equation
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                           DWI,
                           scaling_factor=scaling_factor)

        if mask is not None:
            self.mask = mask
        
    @desc.auto_attr
    def DT(self):
        """
        The diffusion tensor parameters estimated from the data
        """
        return dti.Tensor(self.data, self.bvals, self.bvecs.T, self.mask)

    @desc.auto_attr
    def FA(self):
        return self.DT.fa()

    @desc.auto_attr
    def evecs(self):
        return self.DT.evecs

    @desc.auto_attr
    def evals(self):
        return self.DT.evals

    def tensors():
        """
        Generate a volume with tensor objects
        """
        
    def predicted_signal(self):
        pass
    
class FiberModel(BaseModel):
    """
    A class for representing and solving microtrack models
    """
    def __init__(self,
                 DWI,
                 FG,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 scaling_factor=SCALE_FACTOR):
        """
        Parameters
        ----------
        DWI: A microtrack.dwi.DWI object, or a list containing: [the name of
             nifti file, from which data should be read, bvecs file, bvals file]
        
        FG: a microtrack.fibers.FiberGroup object, or the name of a pdb file
            containing the fibers to be read in using mtf.fg_from_pdb

        axial_diffusivity: The axial diffusivity of a single fiber population.

        radial_diffusivity: The radial diffusivity of a single fiber population.

        scaling_factor: This scales the b value for the Stejskal/Tanner equation
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                             DWI,
                             scaling_factor=scaling_factor)

        self.axial_diffusivity = axial_diffusivity
        self.radial_diffusivity = radial_diffusivity

        # The only additional thing is that this one also has a fiber group,
        # which is xformed to match the coordinates of the DWI:
        self.FG = FG.xform(self.affine, inplace=False)


    @desc.auto_attr
    def fg_idx(self):
        """
        Indices into the coordinates of the fiber-group
        """
        return self.fg_coords.astype(int)
    
    @desc.auto_attr
    def fg_coords(self):
        """
        All the coords of all the fibers  
        """
        return self.FG.coords

    @desc.auto_attr
    def fg_idx_unique(self):
        return mtu.unique_rows(self.fg_idx)

    
    @desc.auto_attr
    def matrix(self):
        """
        The matrix of fiber-contributions to the DWI signal.
        """
        # Assign some local variables, for shorthand:
        vox_coords = self.fg_idx_unique
        n_vox = vox_coords.shape[-1]
        n_bvecs = self.b_idx.shape[0]
        n_fibers = self.FG.n_fibers
        matrix_dims = np.array([n_vox * n_bvecs, n_fibers])
        matrix_len = matrix_dims.prod()

        # Preallocate these:
        matrix_sig = np.zeros(matrix_len)
        matrix_row = np.zeros(matrix_len)
        matrix_col = np.zeros(matrix_len)
        
        for f_idx, fiber in enumerate(self.FG.fibers):
            start = f_idx * n_vox * n_bvecs
            # These are easy:
            matrix_row[start:start+matrix_dims[0]] = (np.arange(matrix_dims[0]))
            matrix_col[start:start+matrix_dims[0]] = f_idx * np.ones(n_vox *
                                                                     n_bvecs)
            # Here comes the tricky part:
            print "working on fiber %s"%(f_idx + 1)
            fiber_idx =  fiber.coords.astype(int)
            fiber_pred = fiber.predicted_signal(
                                self.bvecs[:, self.b_idx],
                                self.bvals[:, self.b_idx],
                                self.axial_diffusivity,
                                self.radial_diffusivity,
                                self.S0[fiber_idx[0],
                                        fiber_idx[1],
                                        fiber_idx[2]]
                                ).ravel()
            # Do we really have to do this one-by-one?
            for i in xrange(fiber_idx.shape[-1]):
                arr_list = [np.where(self.fg_idx_unique[j]==fiber_idx[:,i][j])[0]
                            for j in [0,1,2]]
                this_idx = mtu.intersect(arr_list)
                # Sum the signals from all the fibers/all the nodes in each
                # voxel, by summing over the predicted signal from each fiber
                # through all its
                # nodes and just adding it in:
                for k in this_idx:
                    matrix_sig[start + k * n_bvecs:
                               start + k * n_bvecs + n_bvecs] += \
                        fiber_pred[i:i+n_bvecs]

        #Put it all in one sparse matrix:
        return sps.coo_matrix((matrix_sig,[matrix_row, matrix_col]))
    
    @desc.auto_attr
    def sig(self):
        """
        The signal in the voxels corresponding to where the fibers pass through.
        """ 
        return self.S_weighted[self.fg_idx_unique[0],
                               self.fg_idx_unique[1],
                               self.fg_idx_unique[2]].ravel()