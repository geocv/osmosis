"""

This module is used to construct and solve models of diffusion data 

"""
import inspect
import os
import warnings
import itertools

import numpy as np
import numpy.linalg as npla

# Get parallel computing stuff from IPython:
from IPython import parallel

# We want to try importing numexpr for some array computations, but we can do
# without:
try:
    import numexpr
    has_numexpr = True
except ImportError:
    warnings.warn("Could not import numexpr")
    has_numexpr = False
    
# Import stuff for sparse matrices:
import scipy.sparse as sps
import scipy.sparse.linalg as sla

# Get stuff from sklearn, if that's available: 
try:
    # Get both the sparse version of the Lasso: 
    from sklearn.linear_model.sparse import Lasso as spLasso
    # And the dense version:
    from sklearn.linear_model import Lasso, LassoCV
    # Get other stuff from sklearn.linear_model:
    from sklearn.linear_model import ElasticNet, Lars, Ridge
    # Get OMP:
    from sklearn.linear_model.omp import OrthogonalMatchingPursuit as OMP
     
    has_sklearn = True

    # Make a dict with solvers to be used for choosing among them:
    sklearn_solvers = dict(Lasso=Lasso,
                           OMP=OMP,
                           ElasticNet=ElasticNet,
                           Lars=Lars)

except ImportError:
    warnings.warn("Could not import sklearn")
    has_sklearn = False    

import scipy.linalg as la
import scipy.stats as stats
from scipy.special import sph_harm
import scipy.optimize as opt


import dipy.reconst.dti as dti
import dipy.core.geometry as geo
import dipy.data as dpd
import nibabel as ni

import osmosis.descriptors as desc
import osmosis.fibers as ozf
import osmosis.tensor as ozt
import osmosis.utils as ozu
import osmosis.boot as boot
import osmosis.viz as viz


# Global constants for this module:
AD = 1.5
RD = 0.5
# This converts b values from , so that it matches the units of ADC we use in
# the Stejskal/Tanner equation: 
SCALE_FACTOR = 1000

class DWI(desc.ResetMixin):
    """
    A class for representing dwi data
    """
            
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True
                 ):
        """
        Initialize a DWI object

        Parameters
        ----------
        data: str or array
            The diffusion weighted mr data provided either as a full path to a
            nifti file containing the data, or as a 4-d array.

        bvecs: str or array
            The unit vectors describing the directions of data
            acquisition. Either an 3 by n array, or a full path to a text file
            containing the 3 by n data.

        bvals: str or array
            The values of b weighting in the data acquisition. Either a 1 by n
            array, or a full path to a text file containing the values.
        
        affine: optional, 4 by 4 array
            The affine provided in the file can be overridden by explicitely
            setting this input variable. If this is left as None, one of two
            things will happen. If the 'data' input was a file-name, the affine
            will be read from that file. Otherwise, a warning will be issued
            and affine will default to np.eye(4).

        mask: optional, 3-d array
            When provided, used as a boolean mask into the data for access. 

        sub_sample: int or array of ints.
           If we want to sub-sample the DWI data on the sphere (in the bvecs),
           we can do one of two things: 
           
        1. If sub_sample is an integer, that number of random bvecs will be
           chosen from the data.

        2. If an array of indices is provided, these will serve as indices into
        the last dimension of the data and only that part of the data will be
        used

        verbose: boolean, optional.
           Whether or not to print out various messages as you go
           along. Default: True

        """
        self.verbose=verbose
        
        # All inputs are handled essentially the same. Inputs can be either
        # strings, in which case file reads are required, or arrays, in which
        # case no file reads are needed and we assign these arrays into the
        # attributes:
        for name, val in zip(['data', 'bvecs', 'bvals'],
                             [data, bvecs, bvals]): 
            if isinstance(val, str):
                exec("self.%s_file = '%s'"% (name, val))
            elif isinstance(val, np.ndarray):
                # This time we need to give it the name-space:
                exec("self.%s = val"% name, dict(self=self, val=val))
            else:
                e_s = "%s seems to be neither an array, "% name
                e_s += "nor a file-name\n"
                e_s += "The value provided was: %s" % val
                raise ValueError(e_s)

        # You might have to scale the bvalues by some factor, so that the units
        # come out correctly in the adc calculation:
        self.bvals /= scaling_factor
        
        # You can provide your own affine, if you want and that bypasses the
        # class method provided below as an auto-attr:
        if affine is not None:
            self.affine = np.matrix(affine)

        # If a mask is provided, we will use it to access the data
        if mask is not None:
            # If it's a string, assume it's the full-path to a nifti file with
            # a binary mask: 
            if isinstance(mask, str):
                mask = ni.load(mask).get_data()
            self.mask = np.array(mask, dtype=bool)

        else:
            # Spatial mask (take only the spatial dimensions):
            self.mask = np.ones(self.shape[:3], dtype=bool)

        if sub_sample is not None:
            if np.iterable(sub_sample):
                idx = sub_sample
            else:
                idx = boot.subsample(self.bvecs[:,self.b_idx].T, sub_sample)[1]
            
            self.b_idx = self.b_idx[idx]
            # At this point, signal will be taken according to these
            # sub-sampled indices:
            self.data = np.concatenate([self.signal,
                                        self.data[:,:,:,self.b0_idx]],-1)
            
            self.b0_idx = np.arange(len(self.b0_idx))

            self.bvecs = np.concatenate([np.zeros((3,len(self.b0_idx))),
                                        self.bvecs[:, self.b_idx]],-1)

            self.bvals = np.concatenate([np.zeros(len(self.b0_idx)),
                                         self.bvals[self.b_idx]])
            self.b_idx = np.arange(len(self.b0_idx), len(self.b0_idx) + len(idx))


    @desc.auto_attr
    def shape(self):
        """
        Get the shape of the data. If possible, don't even load it from file to
        get that. 
        """

        # It must have been in an array
        if not hasattr(self, 'data_file'):
            # No reason not to refer to it directly:
            return self.data.shape
        
        # The data is in a file, and you might not have loaded it yet:
        else:
            # No need to actually load it yet:
            return ni.load(self.data_file).get_shape()
            
    @desc.auto_attr
    def bvals(self):
        """
        If bvals were not provided as an array, read them from file
        """
        if self.verbose:
            print("Loading from file: %s"%self.bvals_file)

        return np.loadtxt(self.bvals_file)
    
    @desc.auto_attr
    def bvecs(self):
        """
        If bvecs were not provided as an array, read them from file
        """ 
        if self.verbose:
            print("Loading from file: %s"%self.bvecs_file)

        return np.loadtxt(self.bvecs_file)

    @desc.auto_attr
    def data(self):
        """
        Load the data from file
        """
        if self.verbose:
            print("Loading from file: %s"%self.data_file)

        return ni.load(self.data_file).get_data()

    @desc.auto_attr
    def affine(self):
        """
        Get the affine transformation of the data to world coordinates
        (relative to acpc)
        """
        if hasattr(self, 'data_file'):
            # This means that there might be an affine to read in from file.
            return np.matrix(ni.load(self.data_file).get_affine())
        else:
            w_s = "DWI data generated from array. Affine will be set to"
            w_s += " np.eye(4)"
            warnings.warn(w_s)
            return np.matrix(np.eye(4))

    @desc.auto_attr
    def _flat_data(self):
        """
        Get the flat data only in the mask
        """        
        return self.data[self.mask]
               
    @desc.auto_attr
    def _flat_S0(self):
        """
        Get the signal in the b0 scans in flattened form (only in the mask)
        """
        return np.mean(self._flat_data[:,self.b0_idx], -1)


    @desc.auto_attr
    def _flat_signal(self):
        """
        Get the signal in the diffusion-weighted volumes in flattened form
        (only in the mask).
        """
        return self._flat_data[:,self.b_idx]


    def signal_reliability(self,
                           DWI2,
                           correlator=stats.pearsonr,
                           r_idx=0,
                           square=True):
        """
        Calculate the r-squared of the correlator function provided, in each
        voxel (across directions, not including b0) between this class instance
        and another class instance, provided as input.

        Parameters
        ----------
        DWI2: Another DWI class instance, with data that should look the same,
            if there wasn't any noise in the measurement

        correlator: callable. This is a function that calculates a measure of
             correlation (e.g. stats.pearsonr, or stats.linregress)

        r_idx: int,
            points to the location of r within the tuple returned by
            the correlator callable if r_idx is negative, that means that the
            return value is not a tuple and should be treated as the value
            itself.

        square: bool,
            If square is True, that means that the value returned from
            the correlator should be squared before returning it, otherwise,
            the value itself is returned.
            
        """
                
        val = np.empty(self._flat_signal.shape[0])

        for ii in xrange(len(val)):
            if r_idx>=0:
                val[ii] = correlator(self._flat_signal[ii],
                                     DWI2._flat_signal[ii])[r_idx] 
            else:
                val[ii] = correlator(self._flat_signal[ii],
                                     DWI2._flat_signal[ii]) 

        if square:
            if has_numexpr:
                r_squared = numexpr.evaluate('val**2')
            else:
                r_squared = val**2
        else:
            r_squared = val
        
        # Re-package it into a volume:
        out = ozu.nans(self.shape[:3])
        out[self.mask] = r_squared

        out[out<-1]=-1.0
        out[out>1]=1.0

        return out 

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
    def signal(self):
        """
        The signal in b-weighted volumes
        """
        return self.data[...,self.b_idx].squeeze()

    @desc.auto_attr
    def relative_signal(self):
        """
        The signal in each b-weighted volume, relative to the mean
        of the non b-weighted volumes
        """
        # Need to broadcast for this to work:
        signal_rel = self.signal/np.reshape(self.S0, (self.S0.shape + (1,)))
        # Convert infs to nans:
        signal_rel[np.isinf(signal_rel)] = np.nan
        return signal_rel

    @desc.auto_attr
    def _flat_relative_signal(self):
        """
        Get the flat relative signal only in the mask
        """        
        return np.reshape(self.relative_signal[self.mask],
                          (-1, self.b_idx.shape[0]))


    @desc.auto_attr
    def signal_attenuation(self):
        """
        The amount of attenuation of the signal. This is simply: 

           1-relative_signal 

        """
        return 1 - self.relative_signal

    @desc.auto_attr
    def _flat_signal_attenuation(self):
        """

        """
        return 1-self._flat_relative_signal
    

class BaseModel(DWI):
    """
    Base-class for models.
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 params_file=None,
                 verbose=True):
        """
        A base-class for models based on DWI data.

        Parameters
        ----------

        scaling_factor: int, defaults to 1000.
           To get the units in the S/T equation right, how much do we need to
           scale the bvalues provided.
        
        """
        # DWI should already have everything we need: 
        DWI.__init__(self,
                         data,
                         bvecs,
                         bvals,
                         affine=affine,
                         mask=mask,
                         scaling_factor=scaling_factor,
                         sub_sample=sub_sample,
                         verbose=verbose)

        # Introspect to figure out what name the current class has:
        this_class = str(self.__class__).split("'")[-2].split('.')[-1]
        self.params_file = params_file_resolver(self,
                                                this_class,
                                                params_file=params_file)
         
    @desc.auto_attr
    def fit(self):
        """
        Each model will have a model prediction, which is always in this class
        method. This prediction is used in other methods, such as 'residuals'
        and 'r_squared', etc.

        In this particular case, we set fit to be exactly equal to the
        signal. This should make testing easy :-) 
        """
        return self.signal

    @desc.auto_attr
    def _flat_fit(self):
        """
        Extract a flattened version of the fit, defined for masked voxels
        """
        
        return self.fit[self.mask].reshape((-1, self.signal.shape[-1])) 
        

    def _correlator(self, correlator, r_idx=0, square=True):
        """
        Helper function that uses a callable "func" to apply between two 1-d
        arrays. These 1-d arrays can have different outputs and the one we
        always want is the one which is r_idx into the output tuple 
        """

        val = np.empty(self._flat_signal.shape[0])

        for ii in xrange(len(val)):
            if r_idx>=0:
                val[ii] = correlator(self._flat_signal[ii],
                                     self._flat_fit[ii])[r_idx] 
            else:
                val[ii] = correlator(self._flat_signal[ii],
                                     self._flat_fit[ii]) 
        if square:
            if has_numexpr:
                r_squared = numexpr.evaluate('val**2')
            else:
                r_squared = val**2
        else:
            r_squared = val
        
        # Re-package it into a volume:
        out = ozu.nans(self.shape[:3])
        out[self.mask] = r_squared

        out[out<-1]=-1.0
        out[out>1]=1.0

        return out 

    @desc.auto_attr
    def r_squared(self):
        """
        The r-squared ('explained variance') value in each voxel
        """
        return self._correlator(stats.pearsonr, r_idx=0)
    
    @desc.auto_attr
    def R_squared(self):
        """
        The R-squared ('coefficient of determination' from a linear model fit)
        in each voxel
        """
        return self._correlator(stats.linregress, r_idx=2)

    @desc.auto_attr
    def coeff_of_determination(self):
        """
        Explained variance as: 100 *(1-\frac{RMS(residuals)}{RMS(signal)})

        http://en.wikipedia.org/wiki/Coefficient_of_determination
        
        """
        return self._correlator(ozu.coeff_of_determination,
                                r_idx=np.nan,
                                square=False)

    @desc.auto_attr
    def RMSE(self):
        """
        The square-root of the mean of the squared residuals
        """

        # Preallocate the output:

        out = ozu.nans(self.data.shape[:3])
        res = self.residuals[self.mask]
        
        if has_numexpr:
            out[self.mask] = np.sqrt(np.mean(
                             numexpr.evaluate('res ** 2'), -1))
        else:
            out[self.mask] = np.sqrt(np.mean(np.power(res, 2), -1))
        
        return out

    
    @desc.auto_attr
    def residuals(self):
        """
        The prediction-subtracted residual in each voxel
        """
        out = ozu.nans(self.signal.shape)
        sig = self._flat_signal
        fit = self._flat_fit
        
        if has_numexpr:
            out[self.mask] = numexpr.evaluate('sig - fit')

        else:
            out[self.mask] = sig - fit
            
        return out


# The following is a pattern used by many different classes, so we encapsulate
# it in one general function that everyone can use (DRY!):
def params_file_resolver(object, file_name_root, params_file=None):
    """
    Helper function for resolving what the params file name should be for
    several of the model functions for which the params are cached to file

    Parameters
    ----------
    object: the class instance affected by this

    file_name_root: str, the string which will typically be added to the
        file-name of the object's data file in generating the model params file. 

    params_file: str or None
       If a string is provided, this will be treated as the full path to where
       the params file will get saved. This will be defined if the user
       provides this as an input to the class constructor.

    Returns
    -------
    params_file: str, full path to where the params file will eventually be
            saved, once parameter fitting is done.
    
    """
    # If the user provided
    if params_file is not None: 
        return params_file
    else:
        # If our DWI super-object has a file-name, construct a file-name out of
        # that:
        if hasattr(object, 'data_file'):
            path, f = os.path.split(object.data_file)
            # Need to deal with the double-extension in '.nii.gz':
            file_parts = f.split('.')
            name = file_parts[0]
            extension = ''
            for x in file_parts[1:]:
                extension = extension + '.' + x
                params_file = os.path.join(path, name +
                                           file_name_root +
                    extension)
        else:
            # Otherwise give up and make a file right here with a generic
            # name: 
            params_file = '%s.nii.gz'%file_name_root

    return params_file

class TensorModel(BaseModel):

    """
    A class for representing and solving a simple forward model. Just the
    diffusion tensor.
    
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True):
        """
        Parameters
        -----------

        data, bvecs, bvals: see DWI inputs

        scaling_factor: This scales the b value for the Stejskal/Tanner
        equation

        mask: ndarray or file-name
              An array of the same shape as the data, containing a binary mask
              pointing to the locations of voxels that should be analyzed.

        sub_sample: int or array of ints.

           If we want to sub-sample the DWI data on the sphere (in the bvecs),
           we can do one of two things: 

           1. If sub_sample is an integer, that number of random bvecs will be
           chosen from the data.

           2. If an array of indices is provided, these will serve as indices
           into the last dimension of the data and only that part of the data
           will be used


        params_file: A file to cache the initial tensor calculation in. If this
        file already exists, we pull the tensor fit out of it. Otherwise, we
        calculate the tensor fit and save this file with the params of the
        tensor fit. 
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                           data,
                           bvecs,
                           bvals,
                           affine=None,
                           mask=mask,
                           scaling_factor=scaling_factor,
                           sub_sample=sub_sample,
                           params_file=params_file,
                           verbose=verbose) 
        
    @desc.auto_attr
    def model_params(self):
        """
        The diffusion tensor parameters estimated from the data, using dipy.
        If this calculation has already occurred, just load the data from a
        nifti file, which has shape x by y by z by 12, where the last dimension
        is the model params:

        evecs (9) + evals (3)
        
        """
        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading TensorModel params from: %s" %self.params_file)
            return ni.load(self.params_file).get_data()
        else:
            if self.verbose:
                print("Fitting TensorModel params using dipy")
            block = ozu.nans(self.shape[:3] + (12,))
            mp = dti.Tensor(self.data,
                            self.bvals,
                            self.bvecs.T,
                            self.mask).model_params 

            # Make sure it has the right shape (this is necessary because dipy
            # reshapes things under the hood with its masked interface):
            block[self.mask] = np.reshape(mp,(-1,12))
            
            # Save the params for future use: 
            params_ni = ni.Nifti1Image(block, self.affine)
            params_ni.to_filename(self.params_file)
            # And return the params for current use:
            return block

    @desc.auto_attr
    def evecs(self):
        return np.reshape(self.model_params[..., 3:], 
                          self.model_params.shape[:3] + (3,3))

    @desc.auto_attr
    def evals(self):
        return self.model_params[..., :3]

    @desc.auto_attr
    def mean_diffusivity(self):
        #adc/md = (ev1+ev2+ev3)/3
        return self.evals.mean(-1)

    @desc.auto_attr
    def signal_adc(self):
        """
        This is the empirically defined ADC:

        .. math::
        
            ADC = -log \frac{S}{S0}

        """
        out = ozu.nans(self.signal.shape)
        
        out[self.mask] = ((-1/self.bvals[self.b_idx][0]) *
                        np.log(self._flat_relative_signal))

        return out
        
    @desc.auto_attr
    def adc_residuals(self):
        """
        The model-predicted ADC, conpared with the empirical ADC.
        """
        return self.model_adc - self.signal_adc
    
    @desc.auto_attr
    def fractional_anisotropy(self):
        """
        .. math::

            FA = \sqrt{\frac{1}{2}\frac{(\lambda_1-\lambda_2)^2+(\lambda_1-
                        \lambda_3)^2+(\lambda_2-lambda_3)^2}{\lambda_1^2+
                        \lambda_2^2+\lambda_3^2} }

        """
        out = ozu.nans(self.data.shape[:3])
        
        lambda_1 = self.evals[..., 0][self.mask]
        lambda_2 = self.evals[..., 1][self.mask]
        lambda_3 = self.evals[..., 2][self.mask]

        out[self.mask] = ozu.fractional_anisotropy(lambda_1, lambda_2, lambda_3)

        return out

    @desc.auto_attr
    def radial_diffusivity(self):
        return np.mean(self.evals[...,1:],-1)

    @desc.auto_attr
    def axial_diffusivity(self):
        return self.evals[...,0]


    @desc.auto_attr
    def linearity(self):
        out = ozu.nans(self.data.shape[:3])
        out[self.mask] = ozu.tensor_linearity(self.evals[..., 0][self.mask],
                                              self.evals[..., 1][self.mask],
                                              self.evals[..., 2][self.mask])
        return out

    @desc.auto_attr
    def planarity(self):
        out = ozu.nans(self.data.shape[:3])
        out[self.mask] = ozu.tensor_planarity(self.evals[..., 0][self.mask],
                                              self.evals[..., 1][self.mask],
                                              self.evals[..., 2][self.mask])
        return out

    @desc.auto_attr
    def sphericity(self):
        out = ozu.nans(self.data.shape[:3])
        out[self.mask] = ozu.tensor_sphericity(self.evals[..., 0][self.mask],
                                               self.evals[..., 1][self.mask],
                                               self.evals[..., 2][self.mask])
        return out

    # Self Diffusion Tensor, taken from dipy.reconst.dti:
    @desc.auto_attr
    def tensors(self):
        out = ozu.nans(self.evecs.shape)
        evals = self.evals[self.mask]
        evecs = self.evecs[self.mask]
        D_flat = np.empty(evecs.shape)
        for ii in xrange(len(D_flat)):
            Q = evecs[ii]
            L = evals[ii]
            D_flat[ii] = np.dot(Q*L, Q.T)

        out[self.mask] = D_flat
        return out

    @desc.auto_attr
    def model_adc(self):
        out = np.empty(self.signal.shape)
        tensors_flat = self.tensors[self.mask].reshape((-1,3,3))
        adc_flat = np.empty(self.signal[self.mask].shape)

        for ii in xrange(len(adc_flat)):
            adc_flat[ii] = ozt.apparent_diffusion_coef(
                                        self.bvecs[:,self.b_idx],
                                        tensors_flat[ii])

        out[self.mask] = adc_flat
        return out

    @desc.auto_attr
    def fiber_volume_fraction(self):
        """
        Fiber volume fraction estimated using equation 2 in:

        Stikov, N, Perry, LM, Mezer, A, Rykhlevskaia, E, Wandell, BA, Pauly,
        JM, Dougherty, RF (2011) Bound pool fractions complement diffusion
        measures to describe white matter micro and macrostructure. Neuroimage
        54: 1112.
        
        """
        flat_fvf = ozu.fiber_volume_fraction(
            self.fractional_anisotropy[self.mask])

        out = np.empty(self.fractional_anisotropy.shape)
        out[self.mask] = flat_fvf

        return out


    @desc.auto_attr
    def fit(self):
        if self.verbose:
            print("Predicting signal from TensorModel")
        adc_flat = self.model_adc[self.mask]
        fit_flat = np.empty(adc_flat.shape)
        out = np.empty(self.signal.shape)

        for ii in xrange(len(fit_flat)):
            fit_flat[ii] = ozt.stejskal_tanner(self._flat_S0[ii],
                                               self.bvals[:, self.b_idx],
                                               adc_flat[ii])

        out[self.mask] = fit_flat
        return out

def _dyad_stats(tensor_model_list, mask=None, dyad_stat=boot.dyad_coherence,
                average=True):
    """
    Helper function that does most of the work on calcualting dyad statistics
    """
    if mask is None:
        mask = np.ones(tensor_model_list[0].shape[:3])
        
    # flatten the eigenvectors:
    tensor_model_flat=np.array([this.evecs[np.where(mask)] for this in
    tensor_model_list])
    out_flat = np.empty(tensor_model_flat[0].shape[0])

    # Loop over voxels
    for idx in xrange(tensor_model_flat.shape[1]):
        dyad = boot.dyadic_tensor(tensor_model_flat[:,idx,:,:],
                                  average=average)
        out_flat[idx] = dyad_stat(dyad)

    out = ozu.nans(tensor_model_list[0].shape[:3])
    out[np.where(mask)] = out_flat
    return out        
    

def tensor_coherence(tensor_model_list, mask=None):
    """
    Calculate the coherence of the principle diffusion direction over a bunch
    of TensorModel class instances.


    This is $\kappa = 1-\sqrt{\frac{\beta_2 + \beta_3}{\beta_1}}$, where the
    $\beta_i$ are the eigen-values of the dyadic tensor over the list of
    TensorModel class instances provided as input. 
    """
    # This is the default behavior:
    return _dyad_stats(tensor_model_list, mask=mask,
                       dyad_stat=boot.dyad_coherence,
                       average=True)

def tensor_dispersion(tensor_model_list, mask=None):
    """
    Calculate the dispersion (in degrees) of the principle diffusion direction
    over a sample of TensorModel class instances.
    """
    # Calculate the dyad_dispersion instead:
    return _dyad_stats(tensor_model_list, mask=mask,
                       dyad_stat=boot.dyad_dispersion,
                       average=False) # This one needs to know the individual
                                      # dyads, in addition to the average one.


class SphericalHarmonicsModel(BaseModel):
    """
    A class for evaluating spherical harmonic models. This assumes that a CSD
    model was already fit somehow. Presumably by using mrtrix    
    """ 
    
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 model_coeffs,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 verbose=True):
        """
        Initialize a SphericalHarmonicsModel class instance.
        
        Parameters
        ----------
        DWI: osmosis.dwi.DWI class instance.

        model_coefficients: ndarray
           Coefficients for a SH model, organized according to the conventions
           used by mrtrix (see sph_harm_set for details).
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                           data,
                           bvecs,
                           bvals,
                           affine=affine,
                           mask=mask,
                           scaling_factor=scaling_factor,
                           sub_sample=sub_sample,
                           params_file=params_file,
                           verbose=verbose) 

        # If it's a string, assume it's a full path to a nifti file: 
        if isinstance(model_coeffs,str):
            self.model_coeffs = ni.load(model_coeffs).get_data()
        else:
            # Otherwise, it had better be an array:
            self.model_coeffs = model_coeffs

        self.L = self._calculate_L(self.model_coeffs.shape[-1])
        self.n_params = self.model_coeffs.shape[-1]

        self.ad = axial_diffusivity
        self.rd = radial_diffusivity
        
    @desc.auto_attr
    def sph_harm_set(self):
        """
        Calculate the spherical harmonics, provided n parameters (corresponding
        to nc = (L+1) * (L+2)/2 with L being the maximal harmonic degree for
        the set of bvecs of the object

        Note
        ----

        1. This was written according to the documentation of mrtrix's
        'csdeconv'. The following is taken from there:  

          Note that this program makes use of implied symmetries in the
          diffusion profile. First, the fact the relative signal profile is
          real implies that it has conjugate symmetry, i.e. Y(l,-m) = Y(l,m)*
          (where * denotes the complex conjugate). Second, the diffusion
          profile should be antipodally symmetric (i.e. S(x) = S(-x)), implying
          that all odd l components should be zero. Therefore, this program
          only computes the even elements.

          Note that the spherical harmonics equations used here differ slightly
          from those conventionally used, in that the (-1)^m factor has been
          omitted. This should be taken into account in all subsequent
          calculations.

          Each volume in the output image corresponds to a different spherical
          harmonic component, according to the following convention: [0]    
          Y(0,0)  [1] Im {Y(2,2)} [2] Im {Y(2,1)} [3]     Y(2,0) [4] Re
          {Y(2,1)} [5] Re {Y(2,2)}  [6] Im {Y(4,4)} [7] Im {Y(4,3)} etc... 

          
        2. Take heed that it seems that scipy's sph_harm actually has the
        order/degree in reverse order than the convention used by mrtrix, so
        that needs to be taken into account in the calculation below

        """
                
        # Convert to spherical coordinates:
        r,theta,phi = geo.cart2sphere(self.bvecs[0, self.b_idx],
                                      self.bvecs[1, self.b_idx],
                                      self.bvecs[2, self.b_idx])

        # Preallocate:
        b = np.empty((self.model_coeffs.shape[-1], theta.shape[0]))
    
        i = 0;
        # Only even order are taken:
        for order in np.arange(0, self.L + 1, 2): # Go to L, inclusive!
           for degree in np.arange(-order,order+1):
                # In negative degrees, take the imaginary part: 
                if degree < 0:  
                    b[i,:] = np.imag(sph_harm(-1 * degree, order, theta, phi));
                else:
                    b[i,:] = np.real(sph_harm(degree, order, theta, phi));
                i = i+1;
        return b

    @desc.auto_attr
    def odf(self): 
        """        
        Generate a volume with dimensions (x,y,z, n_bvecs) where each voxel has:

        .. math::

          \sum{w_i, b_i}

        Where $b_i$ are the basis set functions defined from the spherical
        harmonics and $w_i$ are the model coefficients estimated with CSD.

        This a unit-less estimate of the orientation distribution function,
        based on the estimation of the SH coefficients. This needs to be
        convolved with a "response function", a canonical tensor, to calculate
        back the estimated signal. 
        """
        volshape = self.model_coeffs.shape[:3] # Disregarding the params
                                               # dimension
        n_vox = np.sum(self.mask) # These are the voxels we'll look at
        n_weights = self.model_coeffs.shape[3]  # This is the params dimension 

        # Reshape it so that we can multiply for all voxels in one fell swoop:
        d = np.reshape(self.model_coeffs[self.mask], (n_vox, n_weights))

        out = np.empty(self.signal.shape)
        
        # multiply these two matrices together for the estimated odf:  
        out[self.mask] = np.asarray(np.matrix(d) *
                                     np.matrix(self.sph_harm_set))

        return out 
    

    @desc.auto_attr
    def response_function(self):
        """
        A canonical tensor that describes the presumed response of a single
        fiber 
        """
        return ozt.Tensor(np.diag([self.ad, self.rd, self.rd]),
                          self.bvecs[:,self.b_idx],
                          self.bvals[self.b_idx])
        

    @desc.auto_attr
    def fit(self):
        """
        This is the signal estimated from the odf.
        """
        if self.verbose:
            print("Predicting signal from SphericalHarmonicsModel")

        # Reshape the odf to be one voxel per row:
        flat_odf = self.odf[self.mask]
        pred_sig = np.empty(flat_odf.shape)

        for vox in range(pred_sig.shape[0]):
            pred_sig[vox] = self.response_function.convolve_odf(
                                                    flat_odf[vox],
                                                    self._flat_S0[vox])

        # Pack it back into a volume shaped thing: 
        out = ozu.nans(self.signal.shape)
        out[self.mask] = pred_sig  
        return out
        
        
    def _calculate_L(self,n):
        """
        Calculate the maximal harmonic order (L), given that you know the
        number of parameters that were estimated. This proceeds according to
        the following logic:

        .. math:: 

           n = \frac{1}{2} (L+1) (L+2)

           \rarrow 2n = L^2 + 3L + 2
           \rarrow L^2 + 3L + 2 - 2n = 0
           \rarrow L^2 + 3L + 2(1-n) = 0

           \rarrow L_{1,2} = \frac{-3 \pm \sqrt{9 - 8 (1-n)}}{2}

           \rarrow L{1,2} = \frac{-3 \pm \sqrt{1 + 8n}}{2}


        Finally, the positive value is chosen between the two options. 
        """

        L1 = (-3 + np.sqrt(1+ 8 *n))/2
        L2 = (-3 - np.sqrt(1+ 8 *n))/2
        
        return max([L1,L2])



class CanonicalTensorModel(BaseModel):
    """
    This is a simplified bi-tensor model, where one tensor is constrained to be a
    sphere and the other is constrained to be a canonical tensor with some
    globally set axial and radial diffusivities (e.g. based on the median ad
    and rd in the 300 highest FA voxels).

    The signal in each voxel can then be described as a linear combination of
    these two factors:

    .. math::
    
       \frac{S}{S_0} = \beta_1 e^{-bval \vec{b} q \vec{b}^t} + \beta_2 e^{-b D_0} 
 
    Where $\vec{b}$ can be chosen to be one of the diffusion-weighting
    directions used in the scan, or taken from some other collection of unit
    vectors.

    Consequently, for a particular choice of $\vec{b}$ we can write this as:

    .. math::

       S = X \beta

    Where: S is the nvoxels x ndirections $b_0$-attenuatedd signal from the
    entire volume, X is a 2 by ndirections matrix, with one column devoted to
    the anistropic contribution from the canonical tensor and the other column
    containing a constant term in all directions, representing the isotropic
    component. We can solve this equation using OLS fitting and derive the RMSE
    for that choice of $\vec{b}$. For each voxel, we can then find the choice
    of $\vec{b}$ that best predicts the signal (in the least-squares
    sense). This determines the estimated PDD in that voxel.
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 mode='relative_signal',
                 iso_diffusivity=3.0,
                 verbose=True):

        """
        Initialize a CanonicalTensorModel class instance.

        Parameters
        ----------

        params_file: str, optional
             full path to the name of the file in which to save the model
             parameters, once a model is fit. 

        over_sample: optional, int.
           Sometimes you might want to probe the sphere at a higher resolution
           than that provided by the measurement. You can do that using two
           possible sources of information. The first is the camino points,
           which ship together with osmosis and are used for the boot-strapping
           (see osmosis.boot). These are used for integers smaller than 150,
           for 246 and for 755. The other sources of information are the
           symmetric spheres provided as part of dipy. These are used if 362 or
           642 are provided. Note that these might be problematic, because they
           contain polar opposite points, so use with caution.

        mode: string, optional
        This can take one of several values, determining the form of the
        regressors and the form of the signal to fit to.

        'relative_signal': The fit is to $\frac{S}{S_0}$ and the regressors
        are the relative signal predicted for a canonical tensor and the
        relative signal predicted for a isotropic diffusivity compartment:

        .. math::

           \frac{S}{S_0} = \beta_1 e^{-bD} + \beta_2 e^{-b\vec{b}Q\vec{b}^t}

    
        'signal_attenuation': The fit is to $1-\frac{S}{S_0}$ and the
        regressors are the signal attenuation for the canonical tensor and the
        signal attenuation due to isotropic diffusion:

        .. math::

             1-\frac{S}{S_0} = \beta_1 (1-e^{-bD}) + \beta_2 (1-e^{-b\vec{b} Q \vec{b}^t})

        'normalize': in this case, we fit to $\frac{S}{S_0}$, but our regressor
        set is normalized to maximum of 1 in each columns. This affects the
        values of the weights, but not the goodness-of-fit. The equation in
        this case is: 

        .. math::

             \frac{S}{S_0} = \beta_1 + \beta_2 \frac{e^{-b\vec{b} Q \vec{b}^t}}{max(e^{-b\vec{b} Q \vec{b}^t})}


        iso_diffusivity: optional, float
            What the diffusivity of the isotropic component should be set
            to. This is only relevant for 'signal_attenuation' and for
            'relative_signal' modes. Defaults to 3
              
        """
        
        # Initialize the super-class:
        BaseModel.__init__(self,
                            data,
                            bvecs,
                            bvals,
                            affine=affine,
                            mask=mask,
                            scaling_factor=scaling_factor,
                            sub_sample=sub_sample,
                            params_file=params_file,
                            verbose=verbose)
        
        self.ad = axial_diffusivity
        self.rd = radial_diffusivity

        if over_sample is not None:
            # Symmetric spheres from dipy: 
            if over_sample in[362, 642]:
                # We want to get these vertices:
                verts, faces = dpd.get_sphere('symmetric%s'%over_sample)
                # Their convention is transposed relative to ours:
                self.rot_vecs = verts.T
            elif over_sample<=150 or over_sample in [246,755]:
                self.rot_vecs = ozu.get_camino_pts(over_sample)
            else:
                e_s = "You asked to sample the sphere in %s"%over_sample
                e_s += " different directions. Can only do that for n<=150"
                e_s += " or n in [246, 362, 642, 755]"
                raise ValueError(e_s)
        else:
            self.rot_vecs = self.bvecs[:,self.b_idx]

        if mode not in ['relative_signal',
                             'signal_attenuation',
                             'normalize']:
            raise ValueError("Not a recognized mode of CanonicalTensorModel")

        self.mode = mode
        self.iso_diffusivity = iso_diffusivity
        
    @desc.auto_attr
    def response_function(self):
        """
        A canonical tensor that describes the presumed response of a single
        fiber 
        """
        bvecs = self.bvecs[:,self.b_idx]
        bvals = self.bvals[self.b_idx]
        return ozt.Tensor(np.diag([self.ad, self.rd, self.rd]), bvecs, bvals)

     
    @desc.auto_attr
    def rotations(self):
        """
        These are the canonical tensors pointing in the direction of each of
        the bvecs in the sampling scheme. If an over sample number was
        provided, we use the camino points to make canonical tensors pointing
        in all these directions (over-sampling the sphere above the resolution
        of the measurement). 
        """
        out = np.empty((self.rot_vecs.shape[-1], self.b_idx.shape[0]))
        
        # We will use the eigen-value/vectors from the response function
        # and rotate them around to each one of these vectors, calculating
        # the predicted signal in the bvecs of the actual measurement (even
        # when over-sampling):
        evals, evecs = self.response_function.decompose
        for idx, bvec in enumerate(self.rot_vecs.T):
            pred_sig = ozt.rotate_to_vector(bvec, evals, evecs,
                        self.bvecs[:, self.b_idx],
                        self.bvals[self.b_idx]).predicted_signal(1)

            if self.mode == 'signal_attenuation':
                # Fit to 1 - S/S0 
                out[idx] = 1 - pred_sig
            elif self.mode == 'relative_signal':
                # Fit to S/S0 using the predicted diffusion attenuated signal:
                out[idx] = pred_sig
            elif self.mode == 'normalize':
                # Normalize your regressors to have a maximum of 1:
                out[idx] = pred_sig / np.max(pred_sig)
            
        return out

    @desc.auto_attr
    def regressors(self):
        """
        Compute the regressors and the signal to fit to, depending on the mode
        you are using  
        """
        
        iso_pred_sig = np.exp(-self.bvals[self.b_idx][0] * self.iso_diffusivity)

        if self.mode == 'signal_attenuation':
            iso_regressor = 1 - iso_pred_sig * np.ones(self.rotations.shape[-1])
            tensor_regressor = self.rotations
            fit_to = self._flat_signal_attenuation.T
        elif self.mode == 'relative_signal':
            iso_regressor = iso_pred_sig * np.ones(self.rotations.shape[-1])
            tensor_regressor = self.rotations
            fit_to = self._flat_relative_signal.T
        elif self.mode == 'normalize':
            # The only difference between this and the above is that the
            # iso_regressor is here set to all 1's, which can affect the
            # weights... 
            iso_regressor = np.ones(self.rotations.shape[-1])
            tensor_regressor = self.rotations
            fit_to = self._flat_relative_signal.T

        return iso_regressor, tensor_regressor, fit_to

    
    @desc.auto_attr
    def ols(self):
        """
        Compute the OLS solution. 
        """
        # Preallocate:
        ols_weights = np.empty((self.rotations.shape[0], 2,
                               self._flat_signal.shape[0]))

        iso_regressor, tensor_regressor, fit_to = self.regressors
        
        for idx in range(ols_weights.shape[0]):
            # The 'design matrix':
            d = np.vstack([tensor_regressor[idx], iso_regressor]).T
            # This is $(X' X)^{-1} X':
            ols_mat = ozu.ols_matrix(d)
            # Multiply to find the OLS solution (fitting to all the voxels in
            # one fell swoop):
            ols_weights[idx] = np.dot(ols_mat, fit_to)
            # ols_weights[idx] = npla.lstsq(d, fit_to)[0]
        return ols_weights


    @desc.auto_attr
    def model_params(self):
        """
        The model parameters.

        Similar to the TensorModel, if a fit has ocurred, the data is cached on
        disk as a nifti file 

        If a fit hasn't occured yet, calling this will trigger a model fit and
        derive the parameters.

        In that case, the steps are as follows:

        1. Perform OLS fitting on all voxels in the mask, with each of the
           $\vec{b}$. Choose only the non-negative weights. 

        2. Find the PDD that most readily explains the data (highest
           correlation coefficient between the data and the predicted signal)
           and use that one to derive the fit for that voxel

        """

        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading params from file: %s"%self.params_file)

            # Get the cached values and be done with it:
            return ni.load(self.params_file).get_data()
        else:
            # Looks like we might need to do some fitting...
            # Get the bvec weights and the isotropic weights
            b_w = self.ols[:,0,:].copy().squeeze()
            i_w = self.ols[:,1,:].copy().squeeze()

            # nan out the places where weights are negative: 
            b_w[np.logical_or(b_w<0, i_w<0)] = np.nan
            i_w[np.logical_or(b_w<0, i_w<0)] = np.nan

            params = np.empty((self._flat_signal.shape[0],3))
            if self.verbose:
                print("Fitting CanonicalTensorModel:")
                prog_bar = viz.ProgressBar(self._flat_signal.shape[0])
                this_class = str(self.__class__).split("'")[-2].split('.')[-1]
                f_name = this_class + '.' + inspect.stack()[0][3]
            # Find the best OLS solution in each voxel:
            for vox in xrange(self._flat_signal.shape[0]):
                # We do this in each voxel (instead of all at once, which is
                # possible...) to not blow up the memory:
                vox_fits = np.empty(self.rotations.shape)
                for rot_i, rot in enumerate(self.rotations):
                    this_relative = ((b_w[rot_i,vox] * rot + i_w[rot_i,vox]) *
                                       self._flat_S0[vox])
                    if self.mode == 'relative_signal' or self.mode=='normalize':
                        vox_fits[rot_i] = this_relative
                    elif self.mode == 'signal_attenuation':
                        vox_fits[rot_i] = 1 - this_relative
                        
                # Find the predicted signal that best matches the original
                # relative signal. That will choose the direction for the
                # tensor we use:
                corrs = ozu.seed_corrcoef(self._flat_signal[vox],
                                          vox_fits)
                idx = np.where(corrs==np.nanmax(corrs))[0]

                # Sometimes there is no good solution (maybe we need to fit
                # just an isotropic to all of these?):
                if len(idx):
                    # In case more than one fits the bill, just choose the
                    # first one:
                    if len(idx)>1:
                        idx = idx[0]
                    
                    params[vox,:] = np.array([idx,
                                              b_w[idx, vox],
                                              i_w[idx, vox]]).squeeze()
                else:
                    params[vox,:] = np.array([np.nan, np.nan, np.nan])

                if self.verbose:
                    prog_bar.animate(vox, f_name=f_name)

            # Save the params for future use: 
            out_params = ozu.nans(self.signal.shape[:3] + (3,))
            out_params[self.mask] = np.array(params).squeeze()
            params_ni = ni.Nifti1Image(out_params, self.affine)
            if self.verbose:
                print("Saving params to file: %s"%self.params_file)
            params_ni.to_filename(self.params_file)

            # And return the params for current use:
            return out_params

    @desc.auto_attr
    def fit(self):
        """
        Predict the signal from the fit of the CanonicalTensorModel
        """
        if self.verbose:
            print("Predicting signal from CanonicalTensorModel")
            
        out_flat = np.empty(self._flat_signal.shape)
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            if ~np.isnan(flat_params[vox, 1]):
                this_fit = (flat_params[vox,1] *
                            self.rotations[flat_params[vox,0]]+
                            flat_params[vox,2]) * self._flat_S0[vox]
                
                if self.mode == 'signal_attenuation':
                    this_fit = 1 - this_fit  

                out_flat[vox]= this_fit
            else:
                out_flat[vox] = np.nan
                
        out = ozu.nans(self.signal.shape)
        out[self.mask] = out_flat

        return out

class CanonicalTensorModelOpt(CanonicalTensorModel):
    """
    This one is supposed to do the same thing as CanonicalTensorModel, except
    here scipy.optimize is used to find the parameters, instead of OLS fitting.
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 mode='relative_signal',
                 verbose=True):
        """
        Initialize a CanonicalTensorModelOpt class instance.

        Same inputs except we do not accept over-sampling, since it has no
        meaning here.
        """
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=None,  # Always None
                                      mode=mode,
                                      verbose=verbose)


    @desc.auto_attr
    def model_params(self):
        """
        Find the model parameters using least-squares optimization.
        """

        params = np.empty((self._flat_signal.shape[0],4))

        if self.verbose:
            print('Fitting CanonicalTensorModelOpt:')
            prog_bar = viz.ProgressBar(self._flat_signal.shape[0])
            this_class = str(self.__class__).split("'")[-2].split('.')[-1]
            f_name = this_class + '.' + inspect.stack()[0][3]
            
        for vox in range(self._flat_signal.shape[0]):
            # Need to set this one in each voxel, so that the optimizer
            # becomes aware of it:
            self._vox_sig = self._flat_signal[vox]
            # Starting conditions
            mean_sig = np.mean(self._flat_signal[vox])
            start_params = 0, 0, mean_sig,mean_sig
            
            # Do the least-squares fitting (setting tolerance to a rather
            # lenient value?):
            this_params, status = opt.leastsq(self.err_func,
                                          start_params,
                                          ftol=10e-5)
            
            params[vox] = this_params

            if self.verbose: 
                prog_bar.animate(vox, f_name=f_name)

        out_params = ozu.nans(self.signal.shape[:3] + (4,))
        out_params[self.mask] = np.array(params).squeeze()

        return out_params
    def err_func(self, params):
        """
        The error function for the fit:
        """
        theta,phi,tensor_w,iso_w = params
        # Convert to cartesian coordinates:
        x,y,z = geo.sphere2cart(1, theta, phi)
        bvec = [x,y,z]
        evals, evecs = self.response_function.decompose
        rot_tensor = ozt.tensor_from_eigs(
            evecs * ozu.calculate_rotation(bvec, evecs[0]),
                   evals, self.bvecs[:,self.b_idx], self.bvals[:,self.b_idx])

        # Relative to an S0=1:
        pred_sig = tensor_w * rot_tensor.predicted_signal(1) + iso_w
        return pred_sig - self._vox_sig



class MultiCanonicalTensorModel(CanonicalTensorModel):
    """
    This model extends CanonicalTensorModel with the addition of another
    canonical tensor. The logic is similar, but the fitting is done for every
    commbination of sphere + n canonical tensors (where n can be set to any
    number > 1, but can't really realistically be estimated for n>2...).
    
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True,
                 mode='relative_signal',
                 n_canonicals=2):
        """
        Initialize a MultiCanonicalTensorModel class instance.
        """
        # Initialize the super-class:
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      mode=mode,
                                      verbose=verbose)
        
        self.n_canonicals = n_canonicals

    @desc.auto_attr
    def rot_idx(self):
        """
        The indices into combinations of rotations of the canonical tensor,
        according to the order we will use them in fitting
        """
        # Use stdlib magic to make the indices into the basis set: 
        pre_idx = itertools.combinations(range(len(self.b_idx)),
                                         self.n_canonicals)

        # Generate all of them and store, so you know where you stand
        rot_idx = []
        for i in pre_idx:
            rot_idx.append(i)

        return rot_idx

    @desc.auto_attr
    def ols(self):
        """
        Compute the design matrices the matrices for OLS fitting and the OLS
        solution. Cache them for reuse in each direction over all voxels.
        """
        ols_weights = np.empty((len(self.rot_idx),
                                self.n_canonicals + 1,
                                self._flat_signal.shape[0]))

        iso_regressor, tensor_regressor, fit_to = self.regressors

        where_are_we = 0
        for row, idx in enumerate(self.rot_idx):                
        # 'row' refers to where we are in ols_weights
            if self.verbose:
                if idx[0]==where_are_we:
                    s = "Starting MultiCanonicalTensorModel fit"
                    s += " for %sth set of basis functions"%(where_are_we) 
                    print (s)
                    where_are_we += 1
            # The 'design matrix':
            d = np.vstack([[tensor_regressor[i] for i in idx],
                           iso_regressor]).T
            # This is $(X' X)^{-1} X':
            ols_mat = ozu.ols_matrix(d)
            # Multiply to find the OLS solution:
            ols_weights[row] = np.array(np.dot(ols_mat, fit_to)).squeeze()

        return ols_weights

    @desc.auto_attr
    def model_params(self):
        """
        The model parameters.

        Similar to the CanonicalTensorModel, if a fit has ocurred, the data is
        cached on disk as a nifti file 

        If a fit hasn't occured yet, calling this will trigger a model fit and
        derive the parameters.

        In that case, the steps are as follows:

        1. Perform OLS fitting on all voxels in the mask, with each of the
           $\vec{b}$ combinations, choosing only sets for which all weights are
           non-negative. 

        2. Find the PDD combination that most readily explains the data (highest
           correlation coefficient between the data and the predicted signal)
           That will be the combination used to derive the fit for that voxel.

        """
        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading params from file: %s"%self.params_file)

            # Get the cached values and be done with it:
            return ni.load(self.params_file).get_data()
        else:
            # Looks like we might need to do some fitting... 

            # Get the bvec weights (we don't know how many...) and the
            # isotropic weights (which are always last): 
            b_w = self.ols[:,:-1,:].copy().squeeze()
            i_w = self.ols[:,-1,:].copy().squeeze()

            # nan out the places where weights are negative: 
            b_w[b_w<0] = np.nan
            i_w[i_w<0] = np.nan

            # Weight for each canonical tensor, plus a place for the index into
            # rot_idx and one more slot for the isotropic weight (at the end)
            params = np.empty((self._flat_signal.shape[0],
                               self. n_canonicals + 2))

            if self.verbose:
                print("Fitting MultiCanonicalTensorModel:")
                prog_bar = viz.ProgressBar(self._flat_signal.shape[0])
                this_class = str(self.__class__).split("'")[-2].split('.')[-1]
                f_name = this_class + '.' + inspect.stack()[0][3]

            # Find the best OLS solution in each voxel:
            for vox in xrange(self._flat_signal.shape[0]):
                # We do this in each voxel (instead of all at once, which is
                # possible...) to not blow up the memory:
                vox_fits = np.empty((len(self.rot_idx), len(self.b_idx)))
                
                for idx, rot_idx in enumerate(self.rot_idx):
                    this_relative = i_w[idx,vox]
                    this_relative += (np.dot(b_w[idx,:,vox],
                    # The tensor regressors are different in cases where we
                    # are fitting to relative/attenuation signal, so grab that
                    # from the regressors attr:
                    np.array([self.regressors[1][x] for x in rot_idx])))

                    if self.mode == 'relative_signal' or self.mode=='normalize':
                        vox_fits[idx] = this_relative
                    elif self.mode == 'signal_attenuation':
                        vox_fits[idx] = 1 - this_relative

                # Find the predicted signal that best matches the original
                # signal attenuation. That will choose the direction for the
                # tensor we use:
                corrs = ozu.seed_corrcoef(self._flat_relative_signal[vox],
                                          vox_fits)
                
                idx = np.where(corrs==np.nanmax(corrs))[0]

                # Sometimes there is no good solution:
                if len(idx):
                    # In case more than one fits the bill, just choose the
                    # first one:
                    if len(idx)>1:
                        idx = idx[0]
                    
                    params[vox,:] = np.hstack([idx,
                        np.array([x for x in b_w[idx,:,vox]]).squeeze(),
                        i_w[idx, vox]])
                else:
                    # In which case we set it to all nans:
                    params[vox,:] = np.hstack([np.nan,
                                               self.n_canonicals * (np.nan,),
                                               np.nan])

                if self.verbose: 
                    prog_bar.animate(vox, f_name=f_name)

            # Save the params for future use: 
            out_params = ozu.nans(self.signal.shape[:3]+
                                        (params.shape[-1],))
            out_params[self.mask] = np.array(params).squeeze()
            params_ni = ni.Nifti1Image(out_params, self.affine)
            if self.verbose:
                print("Saving params to file: %s"%self.params_file)
            params_ni.to_filename(self.params_file)

            # And return the params for current use:
            return out_params

    @desc.auto_attr
    def fit(self):
        """
        Predict the signal attenuation from the fit of the
        MultiCanonicalTensorModel 
        """
        if self.verbose:
            print("Predicting signal from MultiCanonicalTensorModel")

        out_flat = np.empty(self._flat_signal.shape)
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            # If there's a nan in there, just ignore this voxel and set it to
            # all nans:
            if ~np.any(np.isnan(flat_params[vox, 1])):
                b_w = flat_params[vox,1:1+self.n_canonicals]
                i_w = flat_params[vox,-1]
                # This gets saved as a float, but we can safely assume it's
                # going to be an integer:
                rot_idx = self.rot_idx[int(flat_params[vox,0])]

                out_flat[vox]=(np.dot(b_w,
                               np.array([self.rotations[i] for i in rot_idx])) +
                               i_w) * self._flat_S0[vox]
            else:
                out_flat[vox] = np.nan  # This gets broadcast to the right
                                        # length on assigment?
        
        out = ozu.nans(self.signal.shape)
        out[self.mask] = out_flat

        return out

    @desc.auto_attr
    def fit_angle(self):
        """
        The angle between the tensors that were fitted
        """
        out_flat = np.empty(self._flat_signal.shape[0])
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            if ~np.isnan(flat_params[vox][0]):
                idx = [i for i in self.rot_idx[int(flat_params[vox][0])]]
                ang = np.rad2deg(ozu.vector_angle(
                    self.bvecs[:,self.b_idx].T[idx[0]],
                    self.bvecs[:,self.b_idx].T[idx[1]]))

                ang = np.min([ang, 180-ang])
                
                out_flat[vox] = ang
                
        else:
            out_flat[vox] = np.nan
        
        out = ozu.nans(self.signal.shape[:3])
        out[self.mask] = out_flat

        return out

            
class CalibratedCanonicalTensorModel(CanonicalTensorModel):
    """
    This is another extension of the CanonicalTensorModel, which extends the
    interpertation of the different weights, by calibrating the weights to a
    particular ROI.

    Classically, we will use Corpus Callosum, or some part of it as our
    'calibration target'. In CC, we assume that the axial diffusivity of the
    canonical tensor used is the same as the diffusivity (uniform in all
    directions) of the cellular component in that part of the brain. This
    assumption is based on the idea that diffusion along the axis of the axon
    is hindered by the same kind of things that hinder diffusion inside cells:
    membranes of sub-cellular organelles, macro-molecules, etc. 

    Making this assumption we can write our non-linear model for this part of
    the brain as: 

    .. math ::

    \frac{S}{S_0} = \beta e^{-b \lambda_1} + (1-\beta)e^{-b \vec{b}Q\vect{b}^t}

    Where:

    .. math :: 

    $Q = \begin{pmatrix} \lambda_1 & 0 & 0 \\ 0 &\lambda_2 & 0 \\ 0 & 0 &
\lambda_2 \end{pmatrix}$

    is the quadratic form of the canonical tensor. Once we fit \lambda_1,
    \lambda_2 and \beta to the data from the 'calibration target', we 
    can apply these \lambda_i everywhere.

    To do that, we also need to fit the direction of the canonical tensor in
    that location, which adds two parameters to the fit. Importantly, if we
    choose a part of the brain where the direction of the principal diffusion
    direction is known (such as CC), we can reduce the optimization
    substantially, by starting things off with the canonical tensor oriented in
    the L/R direction. 
    
    """

    
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 calibration_roi,
                 params_file=None,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True):
        """
        Initialize a CalibratedCanonicalTensorModel instance.

        Parameters
        ----------

        calibration_roi: full path to a nifti file containing zeros everywhere,
        except ones where the calibration ROI is defined. Should be already
        registered and xformed to the DWI data resolution/alignment. 

        """
        # Initialize the super-class, we set AD and RD to None, to prevent
        # things from going forward before calibration has occurred. This will
        # probably cause an error to be thrown, if calibration doesn't
        # happen. We might want to catch that error and explain it to the
        # user... 
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=None,
                                      radial_diffusivity=None,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      verbose=verbose)


        # This is used to initialize the optimization in each voxel 
        self.start_params = np.pi/2, np.pi/2, 0.5, 1, 0.5
                            #theta, phi, beta, lambda1, lambda2
        self.calibration_roi = calibration_roi
        
    def _err_func(self, params):
        """
        Error function for the non-linear optimization 
        """

        # The fit parameters: 
        theta, phi, beta, lambda1, lambda2 = params

        # Constraints to stabilize the fit (need to use fmin_slsqp and set
        # these as ub and lb inputs)
        # Angles are 0=<theta<=pi 
        if theta>np.pi or theta<0:
            return np.inf
        # ... and -pi<=phi<= pi:
        if phi>np.pi or phi<-np.pi:
            return np.inf
        # No negative diffusivities: 
        if lambda1<0 or lambda2<0:
            return np.inf
        # Some more constraints on diffusivities:
        if lambda1<0.5 or lambda1>1.5:
            return np.inf
        if lambda2 > lambda1:
            return np.inf
        # Weights between 0 and 1:
        if beta>1 or beta<0:
            return np.inf
            
        return self._pred_sig(theta, phi, beta, lambda1, lambda2) - self._vox_sig

    def _pred_sig(self, theta, phi, beta, lambda1, lambda2):
        """
        The predicted signal for a particular setting of the parameters
        """
        response_function = ozt.Tensor([[lambda1, 0, 0],
                                        [0, lambda2, 0],
                                        [0, 0, lambda2]],
                                        self.bvecs[:,self.b_idx],
                                        self.bvals[:,self.b_idx])
                                        
        # Convert theta and phi to cartesian coordinates:
        x,y,z = geo.sphere2cart(1, theta, phi)
        bvec = [x,y,z]
        evals, evecs = response_function.decompose

        rot_tensor = ozt.tensor_from_eigs(
            evecs * ozu.calculate_rotation(bvec, evecs[0]),
            evals, self.bvecs[:,self.b_idx], self.bvals[:,self.b_idx])

        iso_sig = np.exp(-self.bvals[self.b_idx][0] * lambda1)
        tensor_sig =  rot_tensor.predicted_signal(1)

        pred_sig = beta * iso_sig + (1-beta) * tensor_sig

    @desc.auto_attr
    def _calibration_signal(self):
        """
        The attenuated signal (relative to S0), extracted from the calibration
        target ROI and flattened (n_voxels by n_directions)

        """
        roi_mask = ni.load(self.calibration_roi).get_data()
        return np.reshape(self.relative_signal[np.where(roi_mask)],
                          (-1, self.b_idx.shape[0]))
        
    @desc.auto_attr
    def calibrate(self):

        """"
        This is the function to perform the calibration optimization on. When
        this is done, self.AD and self.RD will be set and parameter estimation
        can proceed as in the super-class

        """

        out = np.empty((self._calibration_signal.shape[0], 5))
        if self.verbose:
            print('Calibrating for AD/RD')
            prog_bar = viz.ProgressBar(self._calibration_signal.shape[0])
            this_class = str(self.__class__).split("'")[-2].split('.')[-1]
            f_name = this_class + '.' + inspect.stack()[0][3]

        # These 'internal' settings of the optimizer could in principle be
        # put in another, more accesible place:
        optim_kwds = dict(ftol=1e-5, full_output=True)

        for vox in range(self._calibration_signal.shape[0]):
            # Need to reassign this with each iteration, so the err-function
            # can become aware of it:
            self._vox_sig = self._calibration_signal[vox] 


            # Perform the fitting itself:
            out[vox], cov_x, infodict, mesg, ier = \
            opt.leastsq(self._err_func, self.start_params, **optim_kwds)
            
            if self.verbose: 
                prog_bar.animate(vox, f_name=f_name)

        return out
            

    
class TissueFractionModel(CanonicalTensorModel):
    """
    This is an extension of the CanonicalTensorModel, based on Mezer et al.'s
    measurement of the tissue fraction in different parts of the brain
    [REF?]. The model posits that tissue fraction accounts for non-free water,
    restriced or hindered by tissue components, which can be represented by a
    canonical tensor and a sphere. The rest (1-tf) is free water, which is
    represented by a second sphere (free water).

    Thus, the model is as follows: 

    .. math:

    \begin{pmatrix} D_1 \\ D_2 \\ ... \\D_n \\ TF \end{pmatrix} =

    \begin{pmatrix} T_1 & D_g & D_iso \\ T_2 & D_g & D_iso \\ T_n & D_g & D_iso
    \\ ... & ... & ... \\ \lambda_1 & \lambda_2 & 0 \end{pmatrix}
    \begin{pmatrix} w_1 & w_2 & w_3 \end{pmatrix}

    And w_2, w_3 are the proportions of tissue-hinderd and free water
    respectively. See below for the estimation proceure
    
    Parameters
    ----------

    tissue_fraction: Full path to a file containing the TF, registered to the
    DWI data and resampled to the DWI data resolution.

    """

    def __init__(self,
                 tissue_fraction,
                 data,
                 bvecs,
                 bvals,
                 alpha1,
                 alpha2,
                 water_D=3,
                 gray_D=1,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 verbose=True):
        
        # Initialize the super-class:
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      verbose=verbose)

        self.tissue_fraction = ni.load(tissue_fraction).get_data()

        # Convert the diffusivity constants to signal attenuation:
        self.gray_D = np.exp(-self.bvals[self.b_idx][0] * gray_D)
        self.water_D = np.exp(-self.bvals[self.b_idx][0] * water_D)

        # We're going to grid-search over these:
        self.alpha1 = alpha1
        self.alpha2 = alpha2
        
    @desc.auto_attr
    def _flat_tf(self):
        """
        Flatten the TF

        """
        return self.tissue_fraction[self.mask]


    @desc.auto_attr
    def signal(self):
        """
        The relevant signal here is:

        .. math::

           \begin{pmatrix} \frac{S_1}{S^0_1} \\ \frac{S_2}{S^0_2} \\ ... \\
           \frac{S_3}{S^0_3} \\ TF \end{pmatrix} 
        
        """
        dw_signal = self.data[...,self.b_idx].squeeze()
        tf_signal = np.reshape(self.tissue_fraction,
                               self.tissue_fraction.shape + (1,))

        return np.concatenate([dw_signal, tf_signal], -1)

    @desc.auto_attr
    def relative_signal(self):
        """
        The signal attenuation in each b-weighted volume, relative to the mean
        of the non b-weighted volumes. We add the original TF here as a last
        volume, so that we can compare fit to signal. 

        Note
        ----
        Need to overload this function for this class, so that the TF does not
        get attenuated.  

        """
        dw_att= self.data[...,self.b_idx]/np.reshape(self.S0,
                                                       (self.S0.shape + (1,)))

        tf_signal = np.reshape(self.tissue_fraction,
                               self.tissue_fraction.shape + (1,))

        return np.concatenate([dw_att, tf_signal], -1) 

    @desc.auto_attr
    def model_params(self):
        """
        Fitting the weights for the TissueFractionModel is done as a second
        stage, after done fitting the CanonicalTensorModel.
        
        The logic is as follows:

        The isotropic weight calculated in the previous stage subsumes two
        different components: one is the free water isotropic component and the
        other is a hindered tissue water component.

        .. math::

            \w_{iso} = \w_2 D_g + \w_3 D_{csf}
            
        Where $\w_{iso}$ is the weight for the isotropic component fit for
        the initial fit and $\w_{2,3}$ are the weights of tissue water and
        free water respectively. $D_g \approx 1$ and $D_{csf} \approx 3$ are
        the diffusivities of gray and white matter, respectively. 

        In addition, we know that the tissue water, together with the tensor
        signal should account for the tissue fraction measurement:

        .. math::
        
            TF = \w_1 * \lambda_1 + \w_2 * \lambda_2 

        Where $\w_1$ is the weight for the canonical tensor found in
        CanonicalTensorModel and $\w_2$ is the weight on the tissue isotropic
        component. $\lambda_{1,2}$ are additional relative weights of the two
        components within the tissue  (canonical tensor and tissue
        water). Implicitly, $\lambda_3 = 0$, reflecting the fact that the free
        water is not part of the tissue fraction at all. To find \lambda{i}, we
        perform a grid search over plausible values of these and choose the
        ones that best account for the diffusion and TF signal.

        To find $\w_2$ and $\w_3$, we follow these steps:

        0. We find $\w_1 = \w_{tensor}$ using the CanonicalTensorModel
        
        1. We fix the values of \lambda_1 and \lambda_2 and solve for \w_2:

            \w_2 = \frac{TF - \lambda_1 \w_1}{\lambda2} =

        2. From the first equation above, we can then solve for \w_3:

            \w_3 = 1 - \w_{iso} - \w_2
            
        3. We go back to the expanded model and predict the diffusion and the
        TF data for these values of     

        """

        # Start by getting the params for the underlying CanonicalTensorModel:
        temp_p_file = self.params_file
        self.params_file = params_file_resolver(self,
                                                'CanonicalTensorModel')
        tensor_params = super(TissueFractionModel, self).model_params
        
        # Restore order: 
        self.params_file = temp_p_file

        # The tensor weight is the second parameter in each voxel: 
        w_ten = tensor_params[self.mask, 1]
        # And the isotropic weight is the third:
        w_iso = tensor_params[self.mask, 2]

        w2 = (self._flat_tf - self.alpha1 * w_ten) / self.alpha2
        w3 = (1 - w_ten - w2)

        w2_out = ozu.nans(self.shape[:3])
        w3_out = ozu.nans(self.shape[:3])

        w2_out[self.mask] = w2
        w3_out[self.mask] = w3

        # Return tensor_idx, w1, w2, w3 
        return tensor_params[...,0],tensor_params[...,1], w2_out, w3_out

    
    @desc.auto_attr
    def fit(self):
        """
        Derive the fit of the TissueFractionModel
        """
        if self.verbose:
            print("Predicting signal from TissueFractionModel")

        out_flat = np.empty((self._flat_signal.shape[0],
                            self._flat_signal.shape[1] + 1))

        flat_ten_idx = self.model_params[0][self.mask]
        flat_w1 = self.model_params[1][self.mask]
        flat_w2 = self.model_params[2][self.mask]
        flat_w3 = self.model_params[3][self.mask]

        for vox in xrange(out_flat.shape[0]):
            if ~np.any(np.isnan([flat_w1[vox], flat_w2[vox], flat_w3[vox]])):

                ten = (flat_w1[vox] *
                    np.hstack([self.rotations[flat_ten_idx[vox]], self.alpha1]))

                tissue_water = flat_w2[vox] * np.hstack(
                [self.gray_D * np.ones(self._flat_signal.shape[-1]) ,
                                                      self.alpha2])

                free_water = flat_w3[vox] * np.hstack(
                [self.water_D * np.ones(self._flat_signal.shape[-1]) , 0])
                
                # recover the signal:
                out_flat[vox]= ((ten + tissue_water + free_water) *
                                self._flat_S0[vox])

                # But not for the last item, which doesn't need to be
                # multiplied by S0: 
                out_flat[vox][-1]/=self._flat_S0[vox]
            else:
                out_flat[vox] = np.nan
                
        out = ozu.nans(self.signal.shape)
        out[self.mask] = out_flat

        return out


    @desc.auto_attr
    def RMSE(self):
        """
        We need to overload this to make the shapes to broadcast into make
        sense. XXX Need to consider whether it makes sense to take out our
        overloaded signal and relative_signal above, so we might not need this
        either... 
        """
        out = ozu.nans(self.signal.shape[:3])
        flat_fit = self.fit[self.mask][:,:self.fit.shape[-1]-1]
        flat_rmse = ozu.rmse(self._flat_signal, flat_fit)                
        out[self.mask] = flat_rmse
        return out


def _tensors_from_fiber(f, bvecs, bvals, ad, rd):
        """
        Helper function to get the tensors for each fiber
        """
        return f.tensors(bvecs, bvals, ad, rd)

class FiberModel(BaseModel):
    """
    
    A class for representing and solving predictive models based on
    tractography solutions.
    
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 FG,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 mode='relative_signal',
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None):
        """
        Parameters
        ----------
        
        FG: a osmosis.fibers.FiberGroup object, or the name of a pdb file
            containing the fibers to be read in using ozf.fg_from_pdb

        axial_diffusivity: The axial diffusivity of a single fiber population.

        radial_diffusivity: The radial diffusivity of a single fiber population.
        
        """
        # Initialize the super-class:
        BaseModel.__init__(self,
                            data,
                            bvecs,
                            bvals,
                            affine=affine,
                            mask=mask,
                            scaling_factor=scaling_factor,
                            params_file=params_file,
                            sub_sample=sub_sample)

        self.axial_diffusivity = axial_diffusivity
        self.radial_diffusivity = radial_diffusivity
        self.mode = mode
        # This one also has a fiber group, which is xformed to match the
        # coordinates of the DWI: 
        self.FG = FG.xform(self.affine.getI(), inplace=False)

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
        """
        The *unique* voxel indices
        """
        return ozu.unique_rows(self.fg_idx.T).T

    @desc.auto_attr
    def voxel2fiber(self):
        """
        The first list in the tuple answers the question: Given a voxel (from
        the unique indices in this model), which fibers pass through it?

        The second answers the question: Given a voxel, for each fiber, which
        nodes are in that voxel? 
        """
        # Preallocate for speed:
        
        # Make a voxels by fibers grid. If the fiber is in the voxel, the value
        # there will be 1, otherwise 0:
        v2f = np.zeros((len(self.fg_idx_unique.T), len(self.FG.fibers)))

        # This is a grid of size (fibers, maximal length of a fiber), so that
        # we can capture put in the voxel number in each fiber/node combination:
        v2fn = ozu.nans((len(self.FG.fibers),
                         np.max([f.coords.shape[-1] for f in self.FG])))

        if self.verbose:
            prog_bar = viz.ProgressBar(self.FG.n_fibers)
            this_class = str(self.__class__).split("'")[-2].split('.')[-1]
            f_name = this_class + '.' + inspect.stack()[0][3]

        # In each fiber:
        for f_idx, f in enumerate(self.FG.fibers):
            # In each voxel present in there:
            for vv in f.coords.astype(int).T:
                # What serial number is this voxel in the unique fiber indices:
                voxel_id = np.where((vv[0] == self.fg_idx_unique[0]) *
                                    (vv[1] == self.fg_idx_unique[1]) *
                                    (vv[2] == self.fg_idx_unique[2]))[0]
                # Add that combination to the grid:
                v2f[voxel_id,f_idx] += 1 
                # All the nodes going through this voxel get its number:
                v2fn[f_idx][np.where((f.coords.astype(int)[0]==vv[0]) *
                                     (f.coords.astype(int)[1]==vv[1]) *
                                     (f.coords.astype(int)[2]==vv[2]))]=voxel_id
            
            if self.verbose:
                prog_bar.animate(f_idx, f_name=f_name)

        return v2f,v2fn



    @desc.auto_attr
    def fiber_tensors(self):
        """
        The tensors for each fiber along it's length
        """
        ten = np.empty(len(self.FG.fibers), dtype='object')
        #rc = parallel.Client()
        #lview = rc.load_balanced_view()
        if self.verbose:
            prog_bar = viz.ProgressBar(self.FG.n_fibers)
            this_class = str(self.__class__).split("'")[-2].split('.')[-1]
            f_name = this_class + '.' + inspect.stack()[0][3]


            #ten = lview.map(_tensors_from_fiber, self.FG.fibers,
            #            len(self.FG.fibers) * [self.bvecs[:, self.b_idx]],
            #            len(self.FG.fibers) * [self.bvals[:, self.b_idx]],
            #            len(self.FG.fibers) * [self.axial_diffusivity],
            #            len(self.FG.fibers) * [self.radial_diffusivity],
            #            block=True)

        # In each fiber:
        ## for f_idx, f in enumerate(self.FG):
        ##     ten[f_idx] = _tensors_from_fiber(f,
        ##                                      self.bvecs[:, self.b_idx],
        ##                                      self.bvals[:, self.b_idx],
        ##                                      self.axial_diffusivity,
        ##                                      self.radial_diffusivity
        ##                                      )
        ##     if self.verbose:
        ##         prog_bar.animate(f_idx, f_name=f_name)

        for f_idx, f in enumerate(self.FG):
            ten[f_idx ] = f.tensors(self.bvecs[:, self.b_idx],
                                    self.bvals[:, self.b_idx],
                                    self.axial_diffusivity,
                          self.radial_diffusivity)       
            if self.verbose:
                prog_bar.animate(f_idx, f_name=f_name)
        return ten
        
    @desc.auto_attr
    def matrix(self):
        """
        The matrix of fiber-contributions to the DWI signal.
        """
        # Assign some local variables, for shorthand:
        vox_coords = self.fg_idx_unique.T
        n_vox = self.fg_idx_unique.shape[-1]
        n_bvecs = self.b_idx.shape[0]
        n_fibers = self.FG.n_fibers
        v2f,v2fn = self.voxel2fiber

        # How many fibers in each voxel (this will determine how many
        # components are in the fiber part of the matrix):
        n_unique_f = np.sum(v2f)        
        
        # Preallocate these, which will be used to generate the two sparse
        # matrices:

        # This one will hold the fiber-predicted signal
        f_matrix_sig = np.zeros(n_unique_f * n_bvecs)
        f_matrix_row = np.zeros(n_unique_f * n_bvecs)
        f_matrix_col = np.zeros(n_unique_f * n_bvecs)

        # And this will hold weights to soak up the isotropic component in each
        # voxel: 
        i_matrix_sig = np.zeros(n_vox * n_bvecs)
        i_matrix_row = np.zeros(n_vox * n_bvecs)
        i_matrix_col = np.zeros(n_vox * n_bvecs)
        
        keep_ct1 = 0
        keep_ct2 = 0

        if self.verbose:
            prog_bar = viz.ProgressBar(len(vox_coords))
            this_class = str(self.__class__).split("'")[-2].split('.')[-1]
            f_name = this_class + '.' + inspect.stack()[0][3]

        # In each voxel:
        for v_idx, vox in enumerate(vox_coords):
            # For each fiber:
            for f_idx in np.where(v2f[v_idx])[0]:
                # Sum the signal from each node of the fiber in that voxel: 
                pred_sig = np.zeros(n_bvecs)
                for n_idx in np.where(v2fn[f_idx]==v_idx)[0]:
                    relative_signal =\
                        self.fiber_tensors[f_idx][n_idx].predicted_signal(1)
                    if self.mode == 'relative_signal':
                        # Predict the signal and demean it, so that the isotropic
                        # part can carry that:
                        pred_sig += (relative_signal -
                            np.mean(self.relative_signal[vox[0],vox[1],vox[2]]))
                    elif self.mode == 'signal_attenuation':
                        pred_sig += ((1 - relative_signal) -
                        np.mean(1 - self.relative_signal[vox[0],vox[1],vox[2]]))
                    
            # For each fiber-voxel combination, we now store the row/column
            # indices and the signal in the pre-allocated linear arrays
            f_matrix_row[keep_ct1:keep_ct1+n_bvecs] =\
                np.arange(n_bvecs) + v_idx * n_bvecs
            f_matrix_col[keep_ct1:keep_ct1+n_bvecs] = np.ones(n_bvecs) * f_idx 
            f_matrix_sig[keep_ct1:keep_ct1+n_bvecs] = pred_sig
            keep_ct1 += n_bvecs

            # Put in the isotropic part in the other matrix: 
            i_matrix_row[keep_ct2:keep_ct2+n_bvecs]=\
                np.arange(v_idx*n_bvecs, (v_idx + 1)*n_bvecs)
            i_matrix_col[keep_ct2:keep_ct2+n_bvecs]= v_idx * np.ones(n_bvecs)
            i_matrix_sig[keep_ct2:keep_ct2+n_bvecs] = 1
            keep_ct2 += n_bvecs
            if self.verbose:
                prog_bar.animate(v_idx, f_name=f_name)
        
        # Allocate the sparse matrices, using the more memory-efficient 'csr'
        # format: 
        fiber_matrix = sps.coo_matrix((f_matrix_sig,
                                       [f_matrix_row, f_matrix_col])).tocsr()
        iso_matrix = sps.coo_matrix((i_matrix_sig,
                                       [i_matrix_row, i_matrix_col])).tocsr()

        if self.verbose:
            print("Generated model matrices")

        return (fiber_matrix, iso_matrix)

        
    @desc.auto_attr
    def voxel_signal(self):
        """        
        The signal in the voxels corresponding to where the fibers pass through.
        """
        if self.mode == 'relative_signal':
            return self.relative_signal[self.fg_idx_unique[0],
                                        self.fg_idx_unique[1],
                                        self.fg_idx_unique[2]]

        elif self.mode == 'signal_attenuation':
            return self.signal_attenuation[self.fg_idx_unique[0],
                                           self.fg_idx_unique[1],
                                           self.fg_idx_unique[2]]

    @desc.auto_attr
    def voxel_signal_demeaned(self):
        """        
        The signal in the voxels corresponding to where the fibers pass
        through, with mean removed
        """
        # Get the average, broadcast it back to the original shape and demean,
        # finally ravel again: 
        return(self.voxel_signal.ravel() -
               (np.mean(self.voxel_signal,-1)[np.newaxis,...] +
        np.zeros((len(self.b_idx),self.voxel_signal.shape[0]))).T.ravel())

    
    @desc.auto_attr
    def iso_weights(self):
        """
        Get the weights using scipy.sparse.linalg or sklearn.linear_model.sparse

        """
        if self.verbose:
            show=True
        else:
            show=False

        iso_w, istop, itn, r1norm, r2norm, anorm, acond, arnorm, xnorm, var=\
        sla.lsqr(self.matrix[1], self.voxel_signal.ravel(), show=show,
                 iter_lim=10e10, atol=10e-10, btol=10e-10, conlim=10e10)

        if istop not in [1,2]:
            warnings.warn("LSQR did not properly converge")

        return iso_w
    
    @desc.auto_attr
    def fiber_weights(self):
        """
        Get the weights for the fiber part of the matrix
        """

        fiber_w = opt.nnls(self.matrix[0].todense(),
                           self.voxel_signal_demeaned)[0]
        # fiber_w =  self._Lasso.coef_
        return fiber_w


    ## @desc.auto_attr
    ## def _Lasso(self):
    ##     """
    ##     This is the sklearn spLasso object. XXX Maybe needs some more
    ##     param-settin options...   
    ##     """
    ##     return spLasso().fit(self.matrix[0], self.voxel_signal_demeaned)
    
    @desc.auto_attr
    def _fiber_fit(self):
        """
        This is the fit for the non-isotropic part of the signal:
        """
        # return self._Lasso.predict(self.matrix[0])
        return np.dot(self.matrix[0].todense(), self.fiber_weights)

    @desc.auto_attr
    def _iso_fit(self):
        # We want this to have the size of the original signal which is
        # (n_bvecs * n_vox), so we broadcast across directions in each voxel:
        return (self.iso_weights[np.newaxis,...] +
                np.zeros((len(self.b_idx), self.iso_weights.shape[0]))).T.ravel()


    @desc.auto_attr
    def fit(self):
        """
        The predicted signal from the FiberModel
        """
        # We generate the lasso prediction and in each voxel, we add the
        # offset, according to the isotropic part of the signal, which was
        # removed prior to fitting:
        
        return np.array(self._fiber_fit + self._iso_fit).squeeze()
               
                
class SparseDeconvolutionModel(CanonicalTensorModel):
    """
    Use the lasso to do spherical deconvolution with a canonical tensor basis
    set. 
    """
    def __init__(self,
                 data,
                 bvecs,
                 bvals,
                 solver=None,
                 solver_params=None,
                 params_file=None,
                 axial_diffusivity=AD,
                 radial_diffusivity=RD,
                 affine=None,
                 mask=None,
                 scaling_factor=SCALE_FACTOR,
                 sub_sample=None,
                 over_sample=None,
                 mode='relative_signal',
                 verbose=True):
        """
        Initialize SparseDeconvolutionModel class instance.
        """
        # Initialize the super-class:
        CanonicalTensorModel.__init__(self,
                                      data,
                                      bvecs,
                                      bvals,
                                      params_file=params_file,
                                      axial_diffusivity=axial_diffusivity,
                                      radial_diffusivity=radial_diffusivity,
                                      affine=affine,
                                      mask=mask,
                                      scaling_factor=scaling_factor,
                                      sub_sample=sub_sample,
                                      over_sample=over_sample,
                                      mode=mode,
                                      verbose=verbose)
        
        # For now, the default is Lasso:
        if solver is None:
            self.solver = 'Lasso'
        else:
            self.solver = solver

        self.params_file = params_file_resolver(self,
                                            'SparseDeconvolutionModel%s'%solver,
                                             params_file)


        # This will be passed as kwarg to the solver initialization:
        self.solver_params = solver_params

    @desc.auto_attr
    def model_params(self):
        """
        Use sklearn to fit the parameters:
        """

        # The file already exists: 
        if os.path.isfile(self.params_file):
            if self.verbose:
                print("Loading params from file: %s"%self.params_file)
            # Get the cached values and be done with it:
            return ni.load(self.params_file).get_data()

        else:

            if self.verbose:
                print("Fitting SparseDeconvolutionModel:")
                prog_bar = viz.ProgressBar(self._flat_signal.shape[0])
                this_class = str(self.__class__).split("'")[-2].split('.')[-1]
                f_name = this_class + '.' + inspect.stack()[0][3]

            iso_regressor, tensor_regressor, fit_to = self.regressors

            # One weight for each rotation
            params = np.empty((self._flat_signal.shape[0],
                               self.rotations.shape[0]))

            # We fit the deviations from the mean signal, which is why we also
            # demean each of the basis functions:
            design_matrix = tensor_regressor - np.mean(tensor_regressor, 0)

            # One basis function per column (instead of rows):
            design_matrix = design_matrix.T
            
            for vox in xrange(self._flat_signal.shape[0]):
                # Fit the deviations from the mean of the fitted signal: 
                sig = fit_to.T[vox] - np.mean(fit_to.T[vox])
                solver = LassoCV()
                params[vox] = solver.fit(design_matrix, sig).coef_
                if self.verbose:
                    prog_bar.animate(vox, f_name=f_name)

            out_params = ozu.nans((self.signal.shape[:3] + 
                                          (design_matrix.shape[-1],)))

            out_params[self.mask] = params
            # Save the params to a file: 
            params_ni = ni.Nifti1Image(out_params, self.affine)
            if self.verbose:
                print("Saving params to file: %s"%self.params_file)
            params_ni.to_filename(self.params_file)

            # And return the params for current use:
            return out_params
            

    @desc.auto_attr
    def fit(self):
        """
        Predict the data from the fit of the SparseDeconvolutionModel
        """
        if self.verbose:
            msg = "Predicting signal from SparseDeconvolutionModel"
            msg += " with %s"%self.solver
            print(msg)
        
        iso_regressor, tensor_regressor, fit_to = self.regressors

        design_matrix = tensor_regressor - np.mean(tensor_regressor, 0)
        design_matrix = design_matrix.T
        out_flat = np.empty(self._flat_signal.shape)
        flat_params = self.model_params[self.mask]
        for vox in xrange(out_flat.shape[0]):
            this_relative = (np.dot(flat_params[vox], design_matrix) + 
                            np.mean(fit_to.T[vox]))
            if self.mode == 'relative_signal' or self.mode=='normalize':
                out_flat[vox] = this_relative * self._flat_S0[vox]
            elif self.mode == 'signal_attenuation':
                out_flat[vox] =  (1 - this_relative) * self._flat_S0[vox]
            
        out = ozu.nans(self.signal.shape)
        out[self.mask] = out_flat

        return out

    
