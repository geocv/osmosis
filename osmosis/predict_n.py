"""

This module contains various functions associated with leave n out cross-validation.
All functions use the multi_bvals' and/or SFM's predict function.

"""
import numpy as np
from math import factorial as f
import itertools
import time
import os
import nibabel as nib
import osmosis.utils as ozu

import osmosis.model.sparse_deconvolution as sfm
import osmosis.snr as snr
import osmosis.multi_bvals as sfm_mb
import osmosis.snr as snr

def predict_n(data, bvals, bvecs, mask, n, b_mode):
    """
    Predicts signals for a certain percentage of the vertices.
    
    Parameters
    ----------
    data: 4 dimensional array
        Diffusion MRI data
    bvals: 1 dimensional array
        All b values
    bvecs: 3 dimensional array
        All the b vectors
    mask: 3 dimensional array
        Brain mask of the data
    n: int
        Integer indicating the percent of vertices that you want to predict
    b_mode: str
        'all': if fitting to all b values
        'bvals': if fitting to individual b values
        
    Returns
    -------
    actual: 2 dimensional array
        Actual signals for the predicted vertices
    predicted: 2 dimensional array 
        Predicted signals for the vertices left out of the fit
    """ 
    t1 = time.time()
    bval_list, b_inds, unique_b, rounded_bvals = snr.separate_bvals(bvals)
    _, b_inds_rm0, unique_b_rm0, rounded_bvals_rm0 = snr.separate_bvals(bvals,
                                                             mode = 'remove0')
    all_b_idx = np.squeeze(np.where(rounded_bvals != 0))    
    
    actual = np.empty((np.sum(mask), len(all_b_idx)))
    predicted = np.empty(actual.shape)
    
    # Generate the regressors in the full model from which we choose the
    # regressors in the reduced model from.
    if b_mode == 'all': 
        full_mod = sfm_mb.SparseDeconvolutionModelMultiB(data, bvecs, bvals,
                                                        mask = mask,
                                                        params_file = "temp",
                                 axial_diffusivity = np.array([1.5,1.5,1.5]),
                                radial_diffusivity = np.array([0.5,0.5,0.5]))                 
    for bi in np.arange(len(unique_b[1:])):
        
        if b_mode is "all":
            all_inc_0 = np.arange(len(rounded_bvals))
            bvals_pool = bvals
        elif b_mode is "bvals":
            all_inc_0 = np.concatenate((b_inds[0], b_inds[1:][bi]))
            bvals_pool = rounded_bvals
        
        these_b_inds = b_inds[1:][bi]
        these_b_inds_rm0 = b_inds_rm0[bi]
        vec_pool = np.arange(len(these_b_inds))
        
        # Need to choose random indices so shuffle them!
        np.random.shuffle(vec_pool)
        
        # How many of the indices are you going to leave out at a time?
        num_choose = np.ceil((n/100.)*len(these_b_inds))
                
        for combo_num in np.arange(np.floor(100./n)):
            these_inc0 = list(all_inc_0)
            idx = list(b_inds_rm0[bi])
            low = (combo_num)*num_choose
            high = np.min([(combo_num*num_choose + num_choose), len(vec_pool)])
            vec_pool_inds = vec_pool[low:high]
            #vec_pool_inds = vec_pool[(combo_num)*num_choose:(combo_num*num_choose + num_choose)]
            vec_combo = these_b_inds[vec_pool_inds]
            vec_combo_rm0 = these_b_inds_rm0[vec_pool_inds]
               
            # Remove the chosen indices from the rest of the indices
            for choice_idx in vec_pool_inds:
                these_inc0.remove(these_b_inds[choice_idx])
                idx.remove(these_b_inds_rm0[choice_idx])
            
            for b_idx in np.arange(len(unique_b[1:])):
                if np.logical_and(b_idx != bi, b_mode is "all"):
                    idx = np.concatenate((idx, b_inds_rm0[b_idx]),0)
                
            # Make the list back into an array
            these_inc0 = np.array(these_inc0)
            
            # Isolate the b vectors, b values, and data not including those
            # to be predicted
            these_bvecs = bvecs[:, these_inc0]
            these_bvals = bvals_pool[these_inc0]
            this_data = data[:, :, :, these_inc0]
            
            # Need to sort the indices first before indexing full model's
            # regressors
            si = sorted(idx)
            
            if b_mode is "all":
                mod = sfm_mb.SparseDeconvolutionModelMultiB(this_data, these_bvecs, these_bvals,
                                                 mask = mask, params_file = "temp",
                                       axial_diffusivity = np.array([1.5,1.5,1.5]),
                                       radial_diffusivity = np.array([0.5,0.5,0.5]))
                # Grab regressors from full model's preloaded regressors
                fit_to = full_mod.regressors[0][:, si]
                tensor_regressor = full_mod.regressors[1][:, si][si, :]
                mod.regressors = demean(fit_to, tensor_regressor, mod)
                            
                predicted[:, vec_combo_rm0] = mod.predict(bvecs[:, vec_combo],
                                                             bvals[vec_combo])

            elif b_mode is "bvals":
                mod = sfm.SparseDeconvolutionModel(this_data, these_bvecs,
                                                   these_bvals, mask = mask,
                                                       params_file = "temp",
                                                    axial_diffusivity = 1.5,
                                                   radial_diffusivity = 0.5)
                                       
                predicted[:, vec_combo_rm0] = mod.predict(bvecs[:, vec_combo])[mod.mask]
            actual[:, vec_combo_rm0] = data[mod.mask][:, vec_combo]
            
        t2 = time.time()
        print "This program took %4.2f minutes to run through %3.2f percent."%((t2 - t1)/60,
                                                             100*((bi+1)/len(unique_b[1:])))
    return actual, predicted
    
def demean(fit_to, tensor_regressor, mod):
    """
    This function demeans the signals and tensor regressors.
    
    Parameters
    ----------
    fit_to: 2 dimensional array
        Original signal fitted to.  Size should be equal to the number of voxels by the
        number of directions.
    tensor_regressor: 2 dimensional array
        The predicted signals from the tensor model.  Size should be equal to the number
        of directions fit to by the number of directions fit to.
        
    Returns
    -------
    fit_to: 2 dimensional array
        Original signal fitted to.  Size should be equal to the number of voxels by the
        number of directions.
    tensor_regressor: 2 dimensional array
        The predicted signals from the tensor model.  Size should be equal to the number
        of directions fit to by the number of directions fit to.
    design_matrix: 2 dimensional array
        Demeaned tensor regressors
    fit_to_demeaned: 2 dimensional array
        Demeaned signal fitted to
    fit_to_means:
        The means of the original signal fitted to.
    """
    
    fit_to_demeaned = np.empty(fit_to.shape)
    fit_to_means = np.empty(fit_to.shape)
    design_matrix = np.empty(tensor_regressor.shape)
    
    for bidx, b in enumerate(mod.unique_b):
        for vox in xrange(mod._n_vox):
            # Need to demean everything across the vertices that were fitted to
            fit_to_demeaned[vox, mod.b_inds_rm0[bidx]] = (fit_to[vox, mod.b_inds_rm0[bidx]]
                                                - np.mean(fit_to[vox, mod.b_inds_rm0[bidx]]))
            fit_to_means[vox, mod.b_inds_rm0[bidx]] = np.mean(fit_to[vox, mod.b_inds_rm0[bidx]])
            design_matrix[mod.b_inds_rm0[bidx]] = (tensor_regressor[mod.b_inds_rm0[bidx]]
                                    - np.mean(tensor_regressor[mod.b_inds_rm0[bidx]].T, -1))
                                    
    return [fit_to, tensor_regressor, design_matrix, fit_to_demeaned, fit_to_means]   
    
def predict_bvals(data, bvals, bvecs, mask, b_fit_to, b_predict):
    """
    Predict for each b value.
    
    Parameters
    ----------
    data: 4 dimensional array
        Diffusion MRI data
    bvals: 1 dimensional array
        All b values
    bvecs: 3 dimensional array
        All the b vectors
    mask: 3 dimensional array
        Brain mask of the data
    b_fit_to: int
        Unique b value index of the b value to fit to.
    b_predict: int
        Unique b value index of the b value to predict.
        
    Returns
    -------
    actual: 2 dimensional array
        Actual signals for the predicted vertices
    predicted: 2 dimensional array 
        Predicted signals for the vertices left out of the fit
    """
    
    bval_list, b_inds, unique_b, rounded_bvals = snr.separate_bvals(bvals)
    bval_list_rm0, b_inds_rm0, unique_b_rm0, rounded_bvals_rm0 = snr.separate_bvals(bvals,
                                                                         mode = 'remove0')
    all_inc_0 = np.concatenate((b_inds[0], b_inds[1:][b_fit_to]))
        
    mod = sfm.SparseDeconvolutionModel(data[:,:,:,all_inc_0], bvecs[:,all_inc_0],
                                               rounded_bvals[all_inc_0], mask = mask,
                                                                params_file = 'temp')
    actual = data[mod.mask][0, b_inds[1:][b_predict]]
    predicted = mod.predict(bvecs[:, b_inds[1:][b_predict]])[mod.mask][0]
        
    return actual, predicted

def nchoosek(n,k):
    """
    Finds all the number of unique combinations from choosing groups of k from a pool of n.
    
    Parameters
    ----------
    n: int
        Number of items in the pool you are choosing from
    k: int
        Size of the groups you are choosing from the pool
        
    n!/(k!*(n-k)!)
    """
    return f(n)/f(k)/f(n-k)
    
def choose_AD_RD(AD_start, AD_end, RD_start, RD_end, AD_num, RD_num):
    """
    Parameters
    ----------
    AD_start: int
        Lowest axial diffusivity desired
    AD_end: int
        Highest axial diffusivity desired
    RD_start: int
        Lowest radial diffusivity desired
    RD_end: int
        Highest radial diffusivity desired
    AD_num: int
        Number of different axial diffusivities
    RD_num: int
        Number of different radial diffusivities
        
    Returns
    -------
    AD_combos: obj
        Unique axial diffusivity combinations
    RD_combos: obj
        Unique radial diffusivity combinations
    """
    
    AD_bag = np.linspace(AD_start, AD_end, num = AD_num)
    RD_bag = np.linspace(RD_start, RD_end, num = RD_num)

    AD_combos = list(itertools.combinations(AD_bag, 3))
    RD_combos = list(itertools.combinations(RD_bag, 3))
    
    return AD_combos, RD_combos
    
def predict_RD_AD(AD_start, AD_end, RD_start, RD_end, AD_num, RD_num, data, bvals, bvecs, mask):
    """
    Predicts vertices with different axial and radial diffusivities and finds them
    root-mean-square error (rmse) between the actual values and predicted values.
    
    Parameters
    ----------
    AD_start: int
        Lowest axial diffusivity desired
    AD_end: int
        Highest axial diffusivity desired
    RD_start: int
        Lowest radial diffusivity desired
    RD_end: int
        Highest radial diffusivity desired
    AD_num: int
        Number of different axial diffusivities
    RD_num: int
        Number of different radial diffusivities
    data: 4 dimensional array
        Diffusion MRI data
    bvals: 1 dimensional array
        All b values
    bvecs: 3 dimensional array
        All the b vectors
    mask: 3 dimensional array
        Brain mask of the data
        
    Returns
    -------
    rmse_b: 1 dimensional array
        The rmse from fitting to individual b values
    rmse_mb: 1 dimensional array
        The rmse from fitting to all the b values
    AD_order: list
        The groups of axial diffusivities in the order they were chosen
    RD_order: list
        The groups of radial diffusivities in the order they were chosen
    """
    AD_combos, RD_combos = choose_AD_RD(AD_start, AD_end, RD_start, RD_end, AD_num, RD_num)
    
    AD_order = []
    RD_order = []
    rmse_b = np.empty((np.sum(mask), nchoosek(AD_num,3)*nchoosek(RD_num,3)))
    rmse_mb = np.empty(rmse_b.shape)

    track = 0
    for AD_idx in np.arange(len(AD_combos)):
        for RD_idx in np.arange(len(RD_combos)):
            actual_b, predicted_b = predict_n(data, bvals, bvecs, mask,
                                           np.array(AD_combos[AD_idx]),
                                           np.array(RD_combos[RD_idx]),
                                                               'bvals')
            actual, predicted = predict_n(data, bvals, bvecs, mask,
                                           np.array(AD_combos[AD_idx]),
                                           np.array(RD_combos[RD_idx]),
                                                                 'all')
            
            rmse_b[:, track] = np.sqrt(np.mean((actual_b - predicted_b)**2, -1))
            rmse_mb[:, track] = np.sqrt(np.mean((actual - predicted)**2, -1))
            
            track = track + 1
            
            AD_order.append(AD_combos[AD_idx])
            RD_order.append(RD_combos[RD_idx])
            
    return rmse_b, rmse_mb, AD_order, RD_order
    
def place_predict(files):
    data_path = "/biac4/wandell/data/klchan13/100307/Diffusion/data"
    file_path = "/hsgs/nobackup/klchan13/predict_AD1RD0_sfm"

    # Get file object
    data_file = nib.load(os.path.join(data_path, "data.nii.gz"))
    wm_data_file = nib.load(os.path.join(data_path,"wm_mask_registered.nii.gz"))

    # Get data and indices
    wm_data = wm_data_file.get_data()
    wm_idx = np.where(wm_data==1)

    # Get b values
    bvals = np.loadtxt(os.path.join(data_path, "bvals"))
    bval_list, b_inds, unique_b, rounded_bvals = snr.separate_bvals(bvals/1000)
    all_b_idx = np.squeeze(np.where(rounded_bvals != 0))

    all_predict_brain = ozu.nans((wm_data_file.shape + all_b_idx.shape))
    bvals_predict_brain = ozu.nans((wm_data_file.shape + all_b_idx.shape))
    actual_brain = ozu.nans((wm_data_file.shape + all_b_idx.shape))
    
    # Keep track of files in case there are any missing ones
    i_track = np.ones(1832)
    for f_idx in np.arange(len(files)):
        this_file = files[f_idx]
        if this_file[(len(this_file)-6):len(this_file)] == "nii.gz":
            sub_data = nib.load(os.path.join(file_path, this_file)).get_data()
            if this_file[0:11] == "all_predict":
                i = int(this_file.split(".")[0][11:])
                low = i*70
                high = np.min([(i+1) * 70, int(np.sum(wm_data))])
                all_predict_brain[wm_idx[0][low:high], wm_idx[1][low:high], wm_idx[2][low:high]] = sub_data
            elif this_file[0:13] == "bvals_predict":
                i = int(this_file.split(".")[0][13:])
                low = i*70
                high = np.min([(i+1) * 70, int(np.sum(wm_data))])
                bvals_predict_brain[wm_idx[0][low:high], wm_idx[1][low:high], wm_idx[2][low:high]] = sub_data
            elif this_file[0:10] == "all_actual":
                i = int(this_file.split(".")[0][10:])
                low = i*70
                high = np.min([(i+1) * 70, int(np.sum(wm_data))])
                actual_brain[wm_idx[0][low:high], wm_idx[1][low:high], wm_idx[2][low:high]] = sub_data
            i_track[i] = 0
        
    missing_files = np.squeeze(np.where(i_track))
    rmse_b = np.sqrt(np.mean((actual_brain[wm_idx]
                     - bvals_predict_brain[wm_idx])**2,-1))
    rmse_mb = np.sqrt(np.mean((actual_brain[wm_idx]
                     - all_predict_brain[wm_idx])**2,-1))

    # Save the rmse and predict data
    aff = data_file.get_affine()
    nib.Nifti1Image(all_predict_brain, aff).to_filename("all_predict_brain.nii.gz")
    nib.Nifti1Image(bvals_predict_brain, aff).to_filename("bvals_predict_brain.nii.gz")

    np.save("rmse_b_flat.npy", rmse_b)
    np.save("rmse_mb_flat.npy", rmse_mb)
    
    return missing_files, rmse_b, rmse_mb, all_predict_brain, bvals_predict_brain
