#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Pipeline for direction dependent calibration

import sys, os, glob, re, pickle
import numpy as np
import pyrap.tables as pt
import lsmtool

#######################################################
from LiLF import lib_ms, lib_img, lib_util, lib_log, lib_dd, lib_h5
logger_obj = lib_log.Logger('pipeline-dd-serial.logger')
logger = lib_log.logger
s = lib_util.Scheduler(log_dir = logger_obj.log_dir, dry = False)
w = lib_util.Walker('pipeline-dd-serial.walker')

# parse parset
parset = lib_util.getParset()
parset_dir = parset.get('LOFAR_dd-serial','parset_dir')
userReg = parset.get('model','userReg')

def clean(p, MSs, res='normal', size=[1,1]):
    """
    p = patch name
    mss = list of mss to clean
    size = in deg of the image
    """
    # set pixscale and imsize
    pixscale = MSs.getListObj()[0].getResolution() 
    if res == 'normal':
        pixscale = float('%.1f'%(pixscale/2.5))
    elif res == 'high':
        pixscale = float('%.1f'%(pixscale/3.5))
    elif res == 'low':
        pass # no change

    imsize = [int(size[0]*1.5/(pixscale/3600.)), int(size[1]*1.5/(pixscale/3600.))] # add 50%
    imsize[0] += imsize[0] % 2
    imsize[1] += imsize[1] % 2
    if imsize[0] < 256: imsize[0] = 256
    if imsize[1] < 256: imsize[1] = 256

    logger.debug('Image size: '+str(imsize)+' - Pixel scale: '+str(pixscale))

    if res == 'normal':
        weight = 'briggs -0.3'
        maxuv_l = None
    elif res == 'high':
        weight = 'briggs -0.6'
        maxuv_l = None
    elif res == 'low':
        weight = 'briggs 0'
        maxuv_l = 3500
    else:
        logger.error('Wrong "res": %s.' % str(res))
        sys.exit()

    # clean 1
    logger.info('Cleaning ('+str(p)+')...')
    imagename = 'img/ddcal-'+str(p)
    lib_util.run_wsclean(s, 'wscleanA-'+str(p)+'.log', MSs.getStrWsclean(), name=imagename, \
            size=imsize, scale=str(pixscale)+'arcsec', \
            weight=weight, niter=5000, no_update_model_required='', minuv_l=30, maxuv_l=maxuv_l, mgain=0.85, \
            baseline_averaging=5, parallel_deconvolution=512, auto_threshold=5, \
            join_channels='', fit_spectral_pol=3, channels_out=9, deconvolution_channels=3)

    # make mask
    im = lib_img.Image(imagename+'-MFS-image.fits', userReg=userReg)
    im.makeMask(threshisl = 7, rmsbox=(50,5))

    # clean 2
    logger.info('Cleaning w/ mask ('+str(p)+')...')
    imagename = 'img/ddcalM-'+str(p)
    lib_util.run_wsclean(s, 'wscleanB-'+str(p)+'.log', MSs.getStrWsclean(), name=imagename, do_predict=True, \
            size=imsize, save_source_list='', scale=str(pixscale)+'arcsec', \
            weight=weight, niter=100000, no_update_model_required='', minuv_l=30, maxuv_l=maxuv_l, mgain=0.85, \
            multiscale='', multiscale_scale_bias=0.75, multiscale_scales='0,10,20,40,80', 
            baseline_averaging=5, parallel_deconvolution=512, local_rms='', auto_threshold=0.75, auto_mask=1.5, fits_mask=im.maskname, \
            join_channels='', fit_spectral_pol=3, channels_out=9, deconvolution_channels=3)

    os.system('cat logs/wscleanA-'+str(p)+'.log logs/wscleanB-'+str(p)+'.log | grep "background noise"')


#############################################################
if w.todo('cleaning'):
    logger.info('Cleaning...')
    lib_util.check_rm('ddcal')
    os.makedirs('ddcal/masks')
    os.makedirs('ddcal/plots')
    os.makedirs('ddcal/images')
    os.makedirs('ddcal/solutions')
    os.makedirs('ddcal/skymodels')
    os.makedirs('ddcal/aterm')

    w.done('cleaning')
### DONE

MSs = lib_ms.AllMSs( glob.glob('mss/TC*[0-9].MS'), s )

# make beam
fwhm = MSs.getListObj()[0].getFWHM(freq='mid')
phase_center = MSs.getListObj()[0].getPhaseCentre()
logger.info('Add columns...')

MSs.run('addcol2ms.py -m $pathMS -c SUBTRACTED_DATA -i DATA', log='$nameMS_addcol.log', commandType='python')

##############################################################
# setup initial model
MSs.getListObj()[0].makeBeamReg('ddcal/beam.reg', freq='mid')
beamReg = 'ddcal/beam.reg'
mosaic_image = lib_img.Image(sorted(glob.glob('self/images/wideM-[0-9]-MFS-image.fits'))[-1], userReg = userReg)
if not os.path.exists(mosaic_image.skymodel_cut): mosaic_image.selectCC()

for cmaj in range(2):
    logger.info('Starting major cycle: %i' % cmaj)
    
    if w.todo('c%02i-delimg' % cmaj):
        lib_util.check_rm('img')
        os.makedirs('img')
        w.done('c%02i-delimg' % cmaj)
    ### DONE

    skymodel_cl = 'ddcal/skymodels/skymodel%02i_cluster.txt' % cmaj
    skymodel_cl_skydb = skymodel_cl.replace('.txt','.skydb')

    picklefile = 'ddcal/directions-c%02i.pickle' % cmaj

    if not os.path.exists(picklefile):
        directions = []

        if not os.path.exists('ddcal/masks/regions-c%02i' % cmaj): os.makedirs('ddcal/masks/regions-c%02i' % cmaj)
        if not os.path.exists('ddcal/images/c%02i' % cmaj): os.makedirs('ddcal/images/c%02i' % cmaj)
    
        ### group into patches corresponding to the mask islands
        mask_cl = mosaic_image.imagename.replace('image.fits', 'mask-cl.fits')
        # this mask is with no user region, done to isolate only bight compact sources
        if not os.path.exists(mask_cl): 
            mosaic_image.beamReg = 'ddcal/beam.reg'
            mosaic_image.makeMask(threshisl=7, atrous_do=False, remove_extended_cutoff=0.001, maskname=mask_cl, only_beam=True)
        
        lsm = lsmtool.load(mosaic_image.skymodel_cut)
        #lsm.group(mask_cl, root='Isl')
        lsm.group('tessellate', targetFlux=0.1, root='Isl') # test to keep all sources
        # this regroup sources
        x = lsm.getColValues('RA',aggregate='wmean')
        y = lsm.getColValues('Dec',aggregate='wmean')
        flux = lsm.getColValues('I',aggregate='sum')
        grouper = lib_dd.Grouper(list(zip(x,y)), flux, look_distance=0.2, kernel_size=0.1, grouping_distance=0.03)
        grouper.run()
        clusters = grouper.grouping()
        grouper.plot()
        os.system('mv grouping*png ddcal/plots/')
        patchNames = lsm.getPatchNames()
    
        logger.info('Merging nearby sources...')
        for cluster in clusters:
            patches = patchNames[cluster]
            #print('Merging:', patches)
            if len(patches) > 1:
                lsm.merge(patches.tolist())
    
        # keep track of CC names used for calibrators so not to subtract them afterwards
        cal_names = lsm.getColValues('Name')
    
        lsm.setPatchPositions(method='wmean') # calculate patch weighted centre for tassellation
        positions = lsm.getPatchPositions()
        for name, flux, size in \
                zip( lsm.getPatchNames(), lsm.getColValues('I', aggregate='sum'), lsm.getPatchSizes(units='deg') ):
            direction = lib_dd.Direction(name)
            position = [positions[name][0].deg, positions[name][1].deg ]
            direction.set_position( position, cal=True )
            direction.set_flux(flux, cal=True)
            direction.set_size([size,size], cal=True)
            directions.append(direction)
        directions = [x for _,x in sorted(zip([d.flux_cal for d in directions],directions))][::-1] # reorder with flux

        # write file
        lsm.write(skymodel_cl, format='makesourcedb', clobber=True)
        skymodel_cl_plot = 'ddcal/masks/skymodel%02i_cluster.png' % cmaj
        lsm.plot(fileName=skymodel_cl_plot, labelBy='patch')
        lsm.setColValues('name', [x.split('_')[-1] for x in lsm.getColValues('patch')]) # just for the region - this makes this lsm useless
        lsm.write('ddcal/masks/regions-c%02i/cluster.reg' % cmaj, format='ds9', clobber=True)
        del lsm
    
        # convert to blob
        lib_util.check_rm(skymodel_cl_skydb)
        s.add('makesourcedb outtype="blob" format="<" in="%s" out="%s"' % (skymodel_cl, skymodel_cl_skydb), log='makesourcedb_cl.log', commandType='general' )
        s.run(check=True)
        
        pickle.dump( directions, open( picklefile, "wb" ) )

    else:
        directions = pickle.load( open( picklefile, "rb" ) )

    if w.todo('c%02i-fullsub' % cmaj):
        # subtract everything - ms:CORRECTED_DATA -> ms:SUBTRACTED_DATA
        logger.info('Subtract everything from CORRECTED_DATA and put the result in SUBTRACTED_DATA...')
        MSs.run('DPPP '+parset_dir+'/DPPP-predict.parset msin=$pathMS msin.datacolumn=CORRECTED_DATA msout.datacolumn=SUBTRACTED_DATA \
                pre.operation=subtract pre.sourcedb='+skymodel_cl_skydb, \
                log='$nameMS_sub-c'+str(cmaj)+'.log', commandType='DPPP')

        w.done('c%02i-fullsub' % cmaj)
    ### DONE
 
    for d in directions:
        # arrive down to calibrators of flux = 1 Jy
        if d.flux_cal < 1: break

        logger.info('c%02i - Working on direction: %s (%f Jy - %f deg)' % (cmaj, d.name, d.flux_cal, d.size_cal[0]))

        ### TTESTTESTTEST: empty image
        clean('empty-c'+str(cmaj)+'-'+d.name, MSs, size=(fwhm,fwhm), res='normal')
        ###

        if w.todo('c%02i-%s-shift' % (cmaj,d.name)):
            logger.info('Phase shift and avg...')
            
            lib_util.check_rm('mss-dir')
            os.makedirs('mss-dir')
            # Shift - ms:SUBTRACTED_DATA -> ms:DATA
            MSs.run('DPPP '+parset_dir+'/DPPP-shiftavg.parset msin=$pathMS msout=mss-dir/$nameMS.MS msin.datacolumn=SUBTRACTED_DATA msout.datacolumn=DATA \
                    shift.phasecenter=['+str(d.position_cal[0])+'deg,'+str(d.position_cal[1])+'deg\]', \
                    log='$nameMS_shift-c'+str(cmaj)+'-'+d.name+'.log', commandType='DPPP')
 
            w.done('c%02i-%s-shift' % (cmaj,d.name))
        ### DONE

        MSs_dir = lib_ms.AllMSs( glob.glob('mss-dir/*MS'), s )

        if w.todo('c%02i-%s-predict' % (cmaj,d.name)):
            logger.info('Add ddcal model to MODEL_DATA...')

            # Predict - ms:MODEL_DATA
            MSs_dir.run('DPPP '+parset_dir+'/DPPP-predict.parset msin=$pathMS msin.datacolumn=DATA msout.datacolumn=MODEL_DATA \
                    pre.sourcedb='+skymodel_cl_skydb+' pre.sources=['+d.name+']', \
                    log='$nameMS_pre2-c'+str(cmaj)+'-'+d.name+'.log', commandType='DPPP')

            # Add back the model previously subtracted for this dd-cal
            logger.info('Set DATA = DATA + MODEL_DATA...')
            MSs_dir.run('taql "update $pathMS set DATA = DATA + MODEL_DATA"', \
                    log='$nameMS_taql-c'+str(cmaj)+'-'+d.name+'.log', commandType='general')

            w.done('c%02i-%s-predict' % (cmaj,d.name))
        ### DONE

        if w.todo('c%02i-%s-smooth' % (cmaj,d.name)):
            logger.info('BL-based smoothing...')

            # Smoothing - ms:DATA -> ms:SMOOTHED_DATA
            MSs_dir.run('BLsmooth.py -r -i DATA -o SMOOTHED_DATA $pathMS', \
                    log='$nameMS_smooth-c'+str(cmaj)+'-'+d.name+'.log', commandType='python')    
 
            w.done('c%02i-%s-smooth' % (cmaj,d.name))
        ### DONE

        if w.todo('c%02i-%s-preimage' % (cmaj,d.name)):

            logger.info('Pre-imaging...')
            clean('%s-pre' % (d.name), MSs_dir, res='normal', size=d.size_cal)

            w.done('c%02i-%s-preimage' % (cmaj,d.name))
        ### DONE
        
        # get initial noise
        image = lib_img.Image('img/ddcalM-%s-pre-MFS-image.fits' % (d.name))
        rms_noise_pre = image.getNoise()
        logger.info('RMS noise (init): %f' % (rms_noise_pre))

        for cdd in range(20):

            logger.info('c%02i - %s: Starting dd cycle: %02i' % (cmaj, d.name, cdd))

            ################################################################
            # Calibrate
   
            if w.todo('c%02i-%s-cdd%02i-calibrate' % (cmaj,d.name,cdd)):
                logger.info('%s (cdd: %02i): Calibrate...' % (d.name,cdd))

                # Calibration - ms:SMOOTHED_DATA
                logger.info('Gain calibration...')
                try: solint = [5,3,2][cdd]
                except: solint = 1
                MSs_dir.run('DPPP '+parset_dir+'/DPPP-solG.parset msin=$pathMS msin.datacolumn=SMOOTHED_DATA \
                    sol.h5parm=$pathMS/cal-g.h5 sol.solint='+str(solint), \
                    log='$nameMS_solG-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.log', commandType='DPPP')
    
                # Plot solutions
                lib_util.run_losoto(s, 'g', [ms+'/cal-g.h5' for ms in MSs_dir.getListStr()], \
                    [parset_dir+'/losoto-amp.parset', parset_dir+'/losoto-plot-amp.parset', parset_dir+'/losoto-plot-ph.parset'])
                lib_util.check_rm('ddcal/plots/plots-g-c%i-%s-cdd%i' % (cmaj, d.name, cdd))
                os.system('mv plots-g ddcal/plots/plots-g-c%i-%s-cdd%i' % (cmaj, d.name, cdd))
                lib_util.check_rm('ddcal/solutions/cal-g-c%i-%s-cdd%i.h5' % (cmaj, d.name, cdd))
                os.system('mv cal-g.h5 ddcal/solutions/cal-g-c%i-%s-cdd%i.h5' % (cmaj, d.name, cdd))
                # TODO: this smoothing is NOT correct for non-contiguous observations!

                # correct G - ms:DATA -> ms:CORRECTED_DATA
                logger.info('Correct ph...')
                MSs_dir.run('DPPP '+parset_dir+'/DPPP-correct.parset msin=$pathMS msin.datacolumn=DATA msout.datacolumn=CORRECTED_DATA \
                             cor.parmdb=ddcal/solutions/cal-g-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.h5 cor.correction=phase000', \
                             log='$nameMS_correct-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.log', commandType='DPPP')
                if cdd>2:
                    logger.info('Correct amp...')
                    MSs_dir.run('DPPP '+parset_dir+'/DPPP-correct.parset msin=$pathMS msin.datacolumn=CORRECTED_DATA msout.datacolumn=CORRECTED_DATA \
                        cor.parmdb=ddcal/solutions/cal-g-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.h5 cor.correction=amplitude000', \
                        log='$nameMS_correct-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.log', commandType='DPPP') 

                w.done('c%02i-%s-cdd%02i-calibrate' % (cmaj,d.name,cdd))
            ### DONE

            if w.todo('c%02i-%s-cdd%02i-image' % (cmaj,d.name,cdd)):

                logger.info('%s (cdd: %02i): imaging...' % (d.name, cdd))
                clean('%s-cdd%02i' % (d.name, cdd), MSs_dir, res='normal', size=d.size_cal)

                w.done('c%02i-%s-cdd%02i-image' % (cmaj,d.name,cdd))
            ### DONE
        
            # get noise, if larger than 95% of prev cycle: break
            image = lib_img.Image('img/ddcalM-%s-cdd%02i-MFS-image.fits' % (d.name, cdd))
            rms_noise = image.getNoise()
            logger.info('RMS noise (cdd:%02i): %f' % (cdd,rms_noise))
            if rms_noise > rms_noise_pre and cdd > 4: break
            rms_noise_pre = rms_noise

        # remove the DD-cal from original dataset using new solutions
        if w.todo('c%02i-%s-subtract' % (cmaj,d.name)):
            
            # Add old model - ms:SUBTRACTED_DATA -> ms:SUBTRACTED_DATA
            logger.info('Add old DD-cal model to SUBTRACTED_DATA...')
            MSs.run('DPPP '+parset_dir+'/DPPP-predict.parset msin=$pathMS msin.datacolumn=SUBTRACTED_DATA msout.datacolumn=SUBTRACTED_DATA \
                    pre.operation=add pre.sourcedb='+skymodel_cl_skydb+' pre.sources=['+d.name+']', \
                    log='$nameMS_add-c'+str(cmaj)+'-'+d.name+'.log', commandType='DPPP')

            # Predict new model - ms:MODEL_DATA
            # TODO: find out the best solution among all obtained in the selfcal
            ddcal_skymodel = 'img/ddcalM-%s-cdd%02i-sources.txt' % (d.name, cdd)
            ddcal_skydb = 'img/ddcalM-%s-cdd%02i-sources.skydb' % (d.name, cdd)
            lib_util.check_rm(ddcal_skydb)
            s.add('makesourcedb outtype="blob" format="<" in="%s" out="%s"' % (ddcal_skymodel, ddcal_skydb), \
                    log='makesourcedb_'+d.name+'.log', commandType='general' )
            s.run(check=True)

            logger.info('Predict new DD-cal model in MODEL_DATA...')
            MSs.run('DPPP '+parset_dir+'/DPPP-predict.parset msin=$pathMS msin.datacolumn=MODEL_DATA msout.datacolumn=MODEL_DATA \
                    pre.sourcedb='+ddcal_skydb, \
                    log='$nameMS_prenew-c'+str(cmaj)+'-'+d.name+'.log', commandType='DPPP')

            # Corrput now model - ms:MODEL_DATA -> MODEL_DATA
            logger.info('Corrupt ph...')
            MSs.run('DPPP '+parset_dir+'/DPPP-correct.parset msin=$pathMS msin.datacolumn=MODEL_DATA msout.datacolumn=MODEL_DATA \
                        cor.invert=False cor.parmdb=ddcal/solutions/cal-g-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.h5 cor.correction=phase000 cor.direction=['+d.name+']', \
                        log='$nameMS_correct-c'+str(cmaj)+'-'+d.name+'.log', commandType='DPPP')
            logger.info('Corrupt amp...')
            MSs.run('DPPP '+parset_dir+'/DPPP-correct.parset msin=$pathMS msin.datacolumn=MODEL_DATA msout.datacolumn=MODEL_DATA \
                   cor.invert=False cor.parmdb=ddcal/solutions/cal-g-c'+str(cmaj)+'-'+d.name+'-cdd'+str(cdd)+'.h5 cor.correction=amplitude000 cor.direction=['+d.name+']', \
                   log='$nameMS_correct-c'+str(cmaj)+'-'+d.name+'.log', commandType='DPPP') 

            # Remove the ddcal again
            logger.info('Set SUBTRACTED_DATA = SUBTRACTED_DATA - MODEL_DATA...')
            MSs.run('taql "update $pathMS set SUBTRACTED_DATA = SUBTRACTED_DATA - MODEL_DATA"', \
                    log='$nameMS_taql-c'+str(cmaj)+'-'+d.name+'.log', commandType='general')

            w.done('c%02i-%s-subtract' % (cmaj,d.name))
        ### DONE

    ######################################################
    # full imaging

    # combine the h5parms
    combined_h5parm = 'ddcal/solutions/combined.h5'
    all_h5parms = ...
    for h5parmFile in all_h5parms:
        dirname = h5parmFile.split('-')[3]
        lib_h5.repoint(h5parmFile, dirname)

    lib_util.check_rm('ddcal/solutions/combined.h5')
    os.system('H5parm_collector.py -o '+combined_h5parm+' '+' '.join(all_h5parms))

    # prepare the aterms
    skymodel = 'ddcal/skymodels/skymodel00_cluster.txt'
    
    box = [phase_center[0], phase_center[1], phase_center[0], phase_center[1]] # [maxRA, minDec, minRA, maxDec]
    os.system('~/scripts/LiLF/scipts/make_aterm_images.py --soltabname gain000 --solsetname sol000 --cellsize_deg 0.1 --smooth_deg 0.1 \
            --bounds_deg %f\;%f\;%f\;%f --bounds_mid_deg %d\;%d --outroot ddcal/aterm/aterm_t --skymodel %s %s' % \
            (*box, *phase_center, skymodel, combined_h5parm) )

    # create aterm config file (ddcal/aterm/aterm.config)
    aterm_config_file = 'ddcal/aterm/aterm.config'
    with open(aterm_config_file, 'w') as file:  # Use file to refer to the file object
        file.write('aterms = [diagonal, beam]')
        file.write('diagonal.images = ['+' '.join(glob.glob('ddcal/aterm/aterm_t*fits'))+']')
        file.write('beam.differential = true\n beam.update_interval = 120\n beam.usechannelfreq = true')

    # run the imager
    imagename = 'img/final-c'+str(cmaj)
    lib_util.run_wsclean(s, 'wsclean-c'+str(cmaj)+'.log', MSs.getStrWsclean(), name=imagename, size='7000 7000', save_source_list='', scale='5arcsec', \
                weight='briggs -0.3', niter=100000, no_update_model_required='', minuv_l=30, mgain=0.85, \
                multiscale='', multiscale_scale_bias=0.75, multiscale_scales='0,10,20,40,80',
                parallel_deconvolution=512, local_rms='', auto_threshold=0.5, auto_mask=1.5, \
                join_channels='', fit_spectral_pol=3, channels_out=9, deconvolution_channels=3, \
                temp_dir='./', pol='I', use_idg='', aterm_config=aterm_config_file, aterm_kernel_size=32, nmiter=4 )
    #wsclean -scale 0.0004166666666666667 -aterm-config ddcal/aterm/aterm.config -multiscale-scales 0 -size 1500 1500 -deconvolution-channels 4 -fits-mask /beegfs/rafferty/Data/LOFAR/Screens/Factor_sim/pipelines/image_1/sector_3/chunk9.ms.premask -auto-mask 3.6 -idg-mode hybrid -channels-out 12 -local-rms-window 50 -mgain 0.5 -minuv-l 80.0 -fit-spectral-pol 3 -maxuv-l 1000000.0 -weighting-rank-filter 3 -aterm-kernel-size 32 -temp-dir /tmp -name /beegfs/rafferty/Data/LOFAR/Screens/Factor_sim/pipelines/image_1/sector_3/chunk9.ms.image -padding 1.2 -pol I -multiscale-shape gaussian -auto-threshold 1.0 -local-rms-method rms-with-min -weight briggs -0.5 -niter 13635 -no-update-model-required -multiscale -fit-beam -reorder -save-source-list -local-rms -join-channels -use-idg -apply-primary-beam -nmiter 4
