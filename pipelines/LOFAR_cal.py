#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Pipeline to run on the calibrator observation.
# It isolates various systematic effects and
# prepare them for the transfer to the target field.

import sys, os, glob, re
import numpy as np

########################################################
from LiLF import lib_ms, lib_img, lib_util, lib_log, lib_h5
logger_obj = lib_log.Logger('pipeline-cal.logger')
logger = lib_log.logger
s = lib_util.Scheduler(log_dir = logger_obj.log_dir, dry = False)
w = lib_util.Walker('pipeline-cal.walker')

# parse parset
parset = lib_util.getParset()
parset_dir = parset.get('LOFAR_cal','parset_dir')
data_dir = parset.get('LOFAR_cal','data_dir')
skymodel = parset.get('LOFAR_cal','skymodel')
imaging = parset.getboolean('LOFAR_cal','imaging')
bl2flag = parset.get('flag','stations')

#############################################################
MSs = lib_ms.AllMSs( glob.glob(data_dir+'/*MS'), s, check_flags = False )

with w.if_todo('copy'):
    # copy data
    logger.info('Copy data...')
    for MS in MSs.getListObj():
        MS.move(MS.nameMS+'.MS', keepOrig=True, overwrite=False)

### DONE

MSs = lib_ms.AllMSs( glob.glob('*MS'), s, check_flags = False )
calname = MSs.getListObj()[0].getNameField()
for MS in MSs.getListObj():
    os.system('cp -r %s %s' % (skymodel, MS.pathMS))

if min(MSs.getFreqs()) < 35.e6:
    iono3rd = True
    logger.debug('Include iono 3rd order.')
else: iono3rd = False

######################################################
# flag bad stations, flags will propagate
with w.if_todo('flag'):
    logger.info("Flagging...")
    MSs.run("DP3 " + parset_dir + "/DP3-flag.parset msin=$pathMS ant.baseline=\"" + bl2flag+"\"", log="$nameMS_flag.log", commandType="DP3")
    
    # extend flags
    logger.info('Remove bad time/freq stamps...')
    MSs.run( 'flagonmindata.py -f 0.5 $pathMS', log='$nameMS_flagonmindata.log', commandType='python')

### DONE

with w.if_todo('predict'):
    # predict to save time ms:MODEL_DATA
    logger.info('Add model to MODEL_DATA (%s)...' % calname)
    MSs.run("DP3 " + parset_dir + "/DP3-predict.parset msin=$pathMS pre.sourcedb=$pathMS/" + os.path.basename(skymodel) + " pre.sources=" + calname, \
            log="$nameMS_pre.log", commandType="DP3", maxThreads=30)

### DONE

###################################################
# 1: find PA

with w.if_todo('cal_pa'):
    # Smooth data DATA -> SMOOTHED_DATA (BL-based smoothing)
    logger.info('BL-smooth...')
    MSs.run('BLsmooth.py -r -c 1 -n 8 -i DATA -o SMOOTHED_DATA $pathMS', log='$nameMS_smooth1.log',
            commandType='python', maxThreads=8)
    
    # Solve cal_SB.MS:SMOOTHED_DATA (only solve)
    logger.info('Calibrating PA...')
    MSs.run('DP3 ' + parset_dir + '/DP3-soldd.parset msin=$pathMS sol.h5parm=$pathMS/pa.h5 sol.mode=rotation+diagonal \
            sol.solint=2 sol.nchan=2',
            log='$nameMS_solPA.log', commandType="DP3")
    
    lib_util.run_losoto(s, 'pa', [ms+'/pa.h5' for ms in MSs.getListStr()],
            [parset_dir+'/losoto-plot-ph.parset', parset_dir+'/losoto-plot-rot.parset', parset_dir+'/losoto-plot-amp.parset', parset_dir+'/losoto-pa.parset'])

    # Pol align correction DATA -> CORRECTED_DATA
    logger.info('Polalign correction...')
    MSs.run('DP3 '+parset_dir+'/DP3-cor.parset msin=$pathMS msin.datacolumn=DATA cor.parmdb=cal-pa.h5 cor.correction=polalign', log='$nameMS_corPA.log', commandType="DP3")
### DONE

########################################################
# 2: find FR

with w.if_todo('cal_fr'):
    # Beam correction CORRECTED_DATA -> CORRECTED_DATA
    logger.info('Beam correction...')
    MSs.run("DP3 " + parset_dir + '/DP3-beam.parset msin=$pathMS corrbeam.updateweights=True', log='$nameMS_beam.log',
            commandType="DP3")

    # Smooth data CORRECTED_DATA -> CIRC_PHASEDIFF_DATA (BL-based smoothing)
    logger.info('BL-smooth...')
    MSs.addcol('CIRC_PHASEDIFF_DATA', 'CORRECTED_DATA', usedysco=False) # need this to make sure no dysco, if we have dyso we cannot set values to zero
    MSs.run('BLsmooth.py -r -c 1 -n 8 -i CIRC_PHASEDIFF_DATA -o CIRC_PHASEDIFF_DATA $pathMS', log='$nameMS_smooth2.log',
            commandType='python', maxThreads=8)

    logger.info('Converting to circular...')
    MSs.run('mslin2circ.py -s -i $pathMS:CIRC_PHASEDIFF_DATA -o $pathMS:CIRC_PHASEDIFF_DATA', log='$nameMS_lincirc.log',
            commandType='python', maxThreads=2)
    # Get circular phase diff CIRC_PHASEDIFF_DATA -> CIRC_PHASEDIFF_DATA
    logger.info('Get circular phase difference...')
    MSs.run('taql "UPDATE $pathMS SET\
     CIRC_PHASEDIFF_DATA[,0]=0.5*EXP(1.0i*(PHASE(CIRC_PHASEDIFF_DATA[,0])-PHASE(CIRC_PHASEDIFF_DATA[,3]))), \
     CIRC_PHASEDIFF_DATA[,3]=CIRC_PHASEDIFF_DATA[,0], \
     CIRC_PHASEDIFF_DATA[,1]=0+0i, \
     CIRC_PHASEDIFF_DATA[,2]=0+0i"',log='$nameMS_taql_phdiff.log', commandType='general')

    logger.info('Creating FR_MODEL_DATA...') # take from MODEL_DATA but overwrite
    MSs.addcol('FR_MODEL_DATA', 'MODEL_DATA', usedysco=False) # need this to make sure no dysco, if we have dyso we cannot set values to zero
    MSs.run('taql "UPDATE $pathMS SET FR_MODEL_DATA[,0]=0.5+0i, FR_MODEL_DATA[,1]=0.0+0i, FR_MODEL_DATA[,2]=0.0+0i, \
     FR_MODEL_DATA[,3]=0.5+0i"', log='$nameMS_taql_frmodel.log', commandType='general')

    # Solve cal_SB.MS:CIRC_PHASEDIFF_DATA against FR_MODEL_DATA (only solve)
    logger.info('Calibrating FR...')
    MSs.run('DP3 ' + parset_dir + '/DP3-soldd.parset msin=$pathMS msin.datacolumn=CIRC_PHASEDIFF_DATA \
     msin.modelcolumn=FR_MODEL_DATA sol.antennaconstraint=[[CS001LBA,CS002LBA,CS003LBA,CS004LBA,CS005LBA,CS006LBA,CS007LBA]] \
    sol.h5parm=$pathMS/fr.h5 sol.mode=phaseonly sol.solint=2 sol.nchan=2 sol.coreconstraint=1e3 sol.smoothnessconstraint=5e6 sol.smoothnessreffrequency=54e6',
            log='$nameMS_solFR.log', commandType="DP3")
    lib_util.run_losoto(s, 'fr', [ms + '/fr.h5' for ms in MSs.getListStr()], [parset_dir + '/losoto-fr.parset'])

    # remove columns
    MSs.run('taql "ALTER TABLE $pathMS DELETE COLUMN CIRC_PHASEDIFF_DATA, FR_MODEL_DATA"', log='$nameMS_taql_delcol.log', commandType='general')

    # Correct FR CORRECTED_DATA -> CORRECTED_DATA
    logger.info('Faraday rotation correction...')
    MSs.run(
        'DP3 ' + parset_dir + '/DP3-cor.parset msin=$pathMS cor.parmdb=cal-fr.h5 cor.correction=rotationmeasure000',
        log='$nameMS_corFR.log', commandType="DP3")
### DONE

#######################################################
## 3: find leak
#
## Smooth data CORRECTED_DATA -> SMOOTHED_DATA (BL-based smoothing)
#logger.info('BL-smooth...')
#MSs.run('BLsmooth.py -r -i CORRECTED_DATA -o SMOOTHED_DATA $pathMS', log='$nameMS_smooth3.log', commandType ='python', maxThreads=10)
#
## Solve cal_SB.MS:SMOOTHED_DATA (only solve)
#logger.info('Calibrating LEAK...')
#MSs.run('DP3 ' + parset_dir + '/DP3-soldd.parset msin=$pathMS sol.h5parm=$pathMS/leak-old.h5 sol.mode=fulljones sol.sourcedb=calib-simple.skydb',\
#        log='$nameMS_solLEAK.log', commandType="DP3")
#
#lib_util.run_losoto(s, 'leak', [ms+'/leak.h5' for ms in MSs.getListStr()], \
#        [parset_dir+'/losoto-plot-amp.parset',parset_dir+'/losoto-plot-ph.parset',parset_dir+'/losoto-leak.parset'])
#
#### TODO: fix for DP3 to apply fulljones
###os.system('losoto -d sol000/amplitude000 cal-leak.h5')
###os.system('losoto -V cal-leak.h5 ~/scripts/LiLF/parsets/LOFAR_cal/losoto-leakfix.parset')
#
## Correct amp LEAK CORRECTED_DATA -> CORRECTED_DATA
#logger.info('Amp/ph Leak correction...')
#MSs.run('DP3 '+parset_dir+'/DP3-cor.parset msin=$pathMS msin.datacolumn=CORRECTED_DATA  cor.parmdb=cal-leak.h5 \
#        cor.correction=fulljones cor.soltab=[amplitudeD,phaseD]', log='$nameMS_corLEAK.log', commandType="DP3")
#sys.exit()

######################################################
# 4: find BP

with w.if_todo('cal_bp'):
    # Smooth data CORRECTED_DATA -> SMOOTHED_DATA (BL-based smoothing)
    logger.info('BL-smooth...')
    MSs.run('BLsmooth.py -r -c 1 -n 8 -i CORRECTED_DATA -o SMOOTHED_DATA $pathMS', log='$nameMS_smooth3.log',
            commandType ='python', maxThreads=8)
    
    # Solve cal_SB.MS:SMOOTHED_DATA (only solve)
    logger.info('Calibrating BP...')
    MSs.run('DP3 ' + parset_dir + '/DP3-soldd.parset msin=$pathMS sol.h5parm=$pathMS/amp.h5 sol.mode=diagonal', log='$nameMS_solAMP.log', commandType="DP3")
    
    lib_util.run_losoto(s, 'amp', [ms+'/amp.h5' for ms in MSs.getListStr()],
            [parset_dir + '/losoto-flag.parset', parset_dir+'/losoto-plot-amp.parset',
             parset_dir+'/losoto-plot-ph.parset', parset_dir+'/losoto-bp.parset'])

### DONE

####################################################
# Re-do correcitons in right order

with w.if_todo('apply_all'):
    # Pol align correction DATA -> CORRECTED_DATA
    logger.info('Polalign correction...')
    MSs.run('DP3 '+parset_dir+'/DP3-cor.parset msin=$pathMS msin.datacolumn=DATA cor.parmdb=cal-pa.h5 cor.correction=polalign', log='$nameMS_corPA2.log', commandType="DP3")
    
    # Correct amp BP CORRECTED_DATA -> CORRECTED_DATA
    logger.info('AmpBP correction...')
    MSs.run('DP3 '+parset_dir+'/DP3-cor.parset msin=$pathMS cor.parmdb=cal-amp.h5 \
            cor.correction=amplitudeSmooth cor.updateweights=True', log='$nameMS_corAMP.log', commandType="DP3")
    
    # Beam correction CORRECTED_DATA -> CORRECTED_DATA
    logger.info('Beam correction...')
    MSs.run("DP3 " + parset_dir + '/DP3-beam.parset msin=$pathMS corrbeam.updateweights=True', log='$nameMS_beam2.log', commandType="DP3")
    
    # Correct LEAK CORRECTED_DATA -> CORRECTED_DATA
    #logger.info('LEAK correction...')
    #MSs.run('DP3 '+parset_dir+'/DP3-cor.parset msin=$pathMS msin.datacolumn=CORRECTED_DATA  cor.parmdb=cal-leak.h5 \
    #        cor.correction=fulljones cor.soltab=[amplitudeD,phaseD]', log='$nameMS_corLEAK2.log', commandType="DP3")
    
    # Correct FR CORRECTED_DATA -> CORRECTED_DATA
    logger.info('Faraday rotation correction...')
    MSs.run('DP3 ' + parset_dir + '/DP3-cor.parset msin=$pathMS cor.parmdb=cal-fr.h5 cor.correction=rotationmeasure000', log='$nameMS_corFR2.log', commandType="DP3")
    
    # Correct abs FR CORRECTED_DATA -> CORRECTED_DATA
    #logger.info('Absolute Faraday rotation correction...')
    #MSs.run('createRMh5parm.py $pathMS $pathMS/rme.h5', log='$nameMS_RME.log', commandType="general", maxThreads=1)
    #MSs.run('DP3 ' + parset_dir + '/DP3-cor.parset msin=$pathMS cor.parmdb=$pathMS/rme.h5 cor.correction=RMextract', log='$nameMS_corRME.log', commandType="DP3")

### DONE

#################################################
# 4: find iono

with w.if_todo('cal_iono'):
    # Smooth data CORRECTED_DATA -> SMOOTHED_DATA (BL-based smoothing)
    logger.info('BL-smooth...')
    MSs.run('BLsmooth.py -r -c 1 -n 8 -i CORRECTED_DATA -o SMOOTHED_DATA $pathMS', log='$nameMS_smooth4.log',
            commandType ='python', maxThreads=8)
    
    # Solve cal_SB.MS:SMOOTHED_DATA (only solve)
    logger.info('Calibrating IONO...')
    MSs.run('DP3 ' + parset_dir + '/DP3-soldd.parset msin=$pathMS sol.h5parm=$pathMS/iono.h5 sol.mode=scalarphase', log='$nameMS_solIONO.log', commandType="DP3")
    
    if iono3rd:
        lib_util.run_losoto(s, 'iono', [ms+'/iono.h5' for ms in MSs.getListStr()],
            [parset_dir+'/losoto-plot-scalarph.parset', parset_dir+'/losoto-iono3rd.parset'])
            #[parset_dir+'/losoto-flag.parset', parset_dir+'/losoto-plot-ph.parset', parset_dir+'/losoto-iono3rd.parset'])
    else:
        lib_util.run_losoto(s, 'iono', [ms+'/iono.h5' for ms in MSs.getListStr()],
            [parset_dir+'/losoto-plot-scalarph.parset', parset_dir+'/losoto-iono.parset'])
            #[parset_dir + '/losoto-flag.parset', parset_dir + '/losoto-plot-ph.parset', parset_dir + '/losoto-iono.parset'])

### DONE

with w.if_todo('compressing_h5'):
    logger.info('Compressing caltables...')
    os.system('cp cal-pa.h5 fullcal-pa.h5')
    #os.system('cp cal-fr.h5 fullcal-fr.h5') # no need to keep orig
    os.system('cp cal-amp.h5 fullcal-amp.h5')
    os.system('cp cal-iono.h5 fullcal-iono.h5')
    s.add('losoto -d sol000/amplitude000 cal-pa.h5', log='losoto-final.log', commandType="python")
    s.run()
    s.add('losoto -d sol000/phase000 cal-pa.h5', log='losoto-final.log', commandType="python")
    s.run()
    s.add('losoto -d sol000/phaseOrig000 cal-pa.h5', log='losoto-final.log', commandType="python")
    s.run()
    os.system('h5repack cal-pa.h5 cal-pa-compressed.h5; mv cal-pa-compressed.h5 cal-pa.h5')
    
    s.add('losoto -d sol000/amplitude000 cal-amp.h5', log='losoto-final.log', commandType="python")
    s.run()
    s.add('losoto -d sol000/amplitudeRes cal-amp.h5', log='losoto-final.log', commandType="python")
    s.run()
    s.add('losoto -d sol000/phase000 cal-amp.h5', log='losoto-final.log', commandType="python")
    s.run()
    os.system('h5repack cal-amp.h5 cal-amp-compressed.h5; mv cal-amp-compressed.h5 cal-amp.h5')
    
    # s.add('losoto -d sol000/tec000 cal-iono.h5', log='losoto-final.log', commandType="python")
    # s.add('losoto -d sol000/clock000 cal-iono.h5', log='losoto-final.log', commandType="python")
    #s.add('losoto -d sol000/amplitude000 cal-iono.h5', log='losoto-final.log', commandType="python")
    s.add('losoto -d sol000/phase_offset000 cal-iono.h5', log='losoto-final.log', commandType="python")
    s.run()
    os.system('h5repack cal-iono.h5 cal-iono-compressed.h5; mv cal-iono-compressed.h5 cal-iono.h5')

### DONE

# a debug image
if imaging:
    logger.info("Imaging section:")

    # Correct all CORRECTED_DATA (PA, beam, FR, BP corrected) -> CORRECTED_DATA
    logger.info('IONO correction...')
    MSs.run("DP3 " + parset_dir + '/DP3-cor.parset msin=$pathMS cor.parmdb=cal-iono.h5 \
        cor.correction=phaseOrig000', log='$nameMS_corIONO.log', commandType="DP3")

    lib_util.check_rm('img')
    os.makedirs('img')

    imgsizepix = int(MSs.getListObj()[0].getFWHM()*3600/5)

    logger.info('Cleaning normal...')
    imagename = 'img/cal'
    lib_util.run_wsclean(s, 'wscleanA.log', MSs.getStrWsclean(), name=imagename, size=imgsizepix, scale='5arcsec',
            weight='briggs 0.', niter=10000, no_update_model_required='', minuv_l=30, mgain=0.85,
            baseline_averaging='', parallel_deconvolution=512,
            auto_threshold=20, join_channels='', fit_spectral_pol=3, channels_out=12, deconvolution_channels=3)

    # make mask
    im = lib_img.Image(imagename+'-MFS-image.fits')
    im.makeMask(threshpix=5)

    logger.info('Cleaning w/ mask...')
    lib_util.run_wsclean(s, 'wscleanB.log', MSs.getStrWsclean(), name=imagename, size=imgsizepix, scale='5arcsec',
            weight='briggs 0.', niter=100000, no_update_model_required='', minuv_l=30, mgain=0.85,
            baseline_averaging='', parallel_deconvolution=512,
            auto_threshold=1, fits_mask=im.maskname, join_channels='', fit_spectral_pol=3, channels_out=12, deconvolution_channels=3)
    os.system('cat logs/wscleanB.log | grep "background noise"')

    # make new mask
    im.makeMask(threshpix=7)

logger.info("Done.")
