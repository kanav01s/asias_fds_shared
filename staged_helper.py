# -*- coding: utf-8 -*-
"""
    Helper functions for foqa-test, base and profile generation and reporting
    Created on Tue Apr 16 15:42:57 2013
    @author: KEITHC
"""
import pdb
import sys, traceback, os, glob, shutil, logging, time, copy, platform
import cPickle as pickle
from datetime import datetime
from collections import OrderedDict
import numpy as np
from hdfaccess.file import hdf_file
from analysis_engine import __version__ as analyzer_version # to check pickle files
from analysis_engine import settings
from analysis_engine.library import np_ma_masked_zeros_like
from analysis_engine.dependency_graph import dependency_order, graph_adjacencies
from analysis_engine.node import (ApproachNode, Attribute,
                                  derived_param_from_hdf,
                                  DerivedParameterNode,
                                  FlightAttributeNode,
                                  KeyPointValueNode,
                                  KeyTimeInstanceNode, Node,
                                  NodeManager, P, Section, SectionNode)
import analysis_engine.node as node
from analysis_engine.process_flight import get_derived_nodes, derive_parameters, geo_locate, _timestamp
import hdfaccess.file

import fds_oracle
import frame_list        # map of tail# to LFLs

logger = logging.getLogger(__name__) #for process_short)_


def get_input_files(INPUT_DIR, file_suffix, logger):
    ''' returns a list of absolute paths '''
    files_to_process = glob.glob(os.path.join(INPUT_DIR, file_suffix))
    file_count = len(files_to_process)
    logger.warning('Processing '+str(file_count)+' files.')
    return files_to_process, file_count


#  from LocalRunner/utils.py
def get_info_from_filename(source_filename, frame_dict):
    '''parse tail number from filename, then lookup aircraft info in framelist'''
    #print 'source_filename: ', source_filename
    root_filename = source_filename.split('.')[0]
    filename_extension = source_filename.split('.')[-1]


    if filename_extension[0] == '0':
        registration = root_filename.split('_')[2]
    elif filename_extension == 'COP' or filename_extension in ['hdf', 'hdf5']:
        registration = source_filename.split('_')[0]
        if len(registration) < 5:
            registration = source_filename.split('_')[2]
    else:
        registration = source_filename.split('_')[0]
        if registration == '':
            registration = source_filename.split('_')[0]            
    frame_details = frame_dict[registration]
    return root_filename, filename_extension, frame_details, registration


def get_short_profile_name(myfile):
    '''peel off last level of folder names in path = profile name'''
    this_path = os.path.realpath(myfile)  #full path to this script
    this_folder =  os.path.split(this_path)[0]
    short_profile = this_folder.replace('\\','/').split('/')[-1]
    return short_profile


def file_move(from_path, to_path):
    '''attempts to move the file even if a file at to_path exists.
       on Windows it will fail if the file at to_path is already open
    '''
    try:
        os.remove(to_path)
    except:
        pass
    os.rename(from_path, to_path)
    return
    

def initialize_logger(LOG_LEVEL, filename='log_messages.txt'):
    '''all stages use this common logger setup'''
    logger = logging.getLogger()
    #logger = initialize_logger(LOG_LEVEL)
    logger.setLevel(LOG_LEVEL)
    logger.addHandler(logging.FileHandler(filename=filename)) #send to file 
    logger.addHandler(logging.StreamHandler())                #tee to screen
    return logger


def clean_up(time_start, file_count, logger, timing_report=None):
    '''wrap up at end of analyze and profile runs'''
    print '  time to process '+ str(file_count)+' files: '+str(time.time() - time_start) 
    print ' *** Processing finished'
    if timing_report:
        timing_report.close()    
    for handler in logger.handlers: handler.close()        


###  plots 
def show_plot(plt):                      
    print 'Paused for plot review. Close plot window to continue.'
    plt.show()
    plt.close()

def save_plot(plt, fname):
    plt.draw()
    plt.savefig(fname, transparent=False ) #, bbox_inches="tight")
    plt.close()

def aplot(array_dict={}, title='array plot', grid=True, legend=True):
    '''plot a dictionary of up to four arrays, with legend by default
        example dict:  {'Airspeed': airspeed.array }
    '''
    import matplotlib.pyplot as plt
    print 'title:', title
    if len(array_dict.keys())==0:
        print 'Nothing to plot!'
        return
    figure = plt.figure()
    figure.set_size_inches(10,5)
    series_names = array_dict.keys()[:4]  #only first 4
    series_formats = ['k','g','b','r']    #color codes
    for i,nm in enumerate(series_names):
        plt.plot(array_dict[nm], series_formats[i])
    if grid: plt.grid(True, color='gray')
    plt.title(title)
    if legend: 
        leg = plt.legend(series_names, 'upper left', fancybox=True)
        leg.get_frame().set_alpha(0.5)        
    plt.xlabel('sample index')
    print 'Paused for plot review. Close plot window to continue.'
    plt.show()
    plt.clf()
    plt.close()


def lplot(myhdf5, params=('Airspeed','Altitude STD')):
    '''plot a list of parameters from an hdf5 file'''
    pdict={}
    for p in params:
        pdict[p]=myhdf5.get(p).array
    aplot(pdict)

### job report 
JOB_REPORT_FIELDS = ['run_time', 'stage',  'profile', 'cmt', 'input_path', 'output_path', 'file_count', 'processing_seconds']

def get_job_record(timestamp, stage, profile, comment, input_path, output_path, file_count, processing_seconds, file_repository='central'):
    '''return job info as an OrderedDict'''
    #computer_name = platform.node()
    rec = OrderedDict([ ('run_time', timestamp),    ('stage', stage), 
                        ('profile', profile),       ('cmt', comment), ('file_repository',file_repository),
                        ('input_path', input_path), ('output_path', output_path),
                        ('file_count', file_count), ('processing_seconds', processing_seconds)
                     ])
    return rec
    

def report_job(timestamp,   stage, profile, comment, input_path, output_path, 
               file_count, processing_seconds, logger,  db_connection=None):
    '''save timing record to csv and oracle (if available)'''
    report_name = settings.PREP_REPORTS_PATH + 'fds_jobs.csv'        
    job_rec = OrderedDict([ ('run_time', timestamp),    ('stage', stage), 
                            ('profile', profile),       ('cmt', comment), 
                            ('input_path', input_path), ('output_path', output_path),
                            ('file_count', file_count), ('processing_seconds', processing_seconds)
                          ]) 
    #print job_rec                                          
    with open(report_name,'a') as rpt:
        rpt.write( ','.join([ str(v) for v in job_rec.values()]) + '\n') 
    if db_connection:            
        dict_to_oracle(db_connection, job_rec, 'fds_jobs')
    logger.warning('\nJOB REPORT: \n' + '\n'.join([str(v) for v in job_rec.items()]) )


### timing report
TIMING_REPORT_FIELDS = ['run_time', 'stage',  'profile', 'cmt', 'source_file', 'file_size_meg', 'processing_seconds', 'epoch', 'status']

def initialize_timing_report(REPORTS_DIR):
    '''for reporting run times '''
    timestamp = datetime.now()
    report_name = REPORTS_DIR + 'fds_processing_time.csv'
    return timestamp, report_name    
   

def report_timing(timestamp, stage, profile, filepath, 
                  processing_time, status, logger, db_connection=None):
    '''save timing record to csv and if oracle (if available)'''
    if profile=='base':
        report_name = settings.PREP_REPORTS_PATH + 'fds_processing_time.csv'        
    else: 
        report_name = settings.PROFILE_REPORTS_PATH + 'fds_processing_time.csv' 

    file_size = os.path.getsize(filepath)/(1024.*1024.)
    filebase = os.path.basename(filepath)
    timing_rec  = OrderedDict([ ('run_time', timestamp),
                       ('stage', stage),
                       ('profile', profile),
                       ('source_file', filebase),
                       ('file_size_meg', file_size),
                       ('processing_seconds', processing_time),
                       ('epoch', time.time()),
                       ('status',status),
                     ])
    #print timing_rec                                          
    with open(report_name,'a') as rpt:
        rpt.write( ','.join([ str(v) for v in timing_rec.values()]) + '\n') 
    if db_connection:            
        dict_to_oracle(db_connection, timing_rec, 'fds_processing_time')
    logger.debug('align report ' + ','.join([str(v) for v in timing_rec.values()]) )


### generic reporting
def record_to_csv(record, dest_path):
    '''append data from a list as a record to a CSV file.  assumes simple fields.'''
    #header = record.keys()
    row =  [ str(v) for v in record]
    with open(dest_path, 'at') as dest:
         dest.write( ','.join(row) +'\n')
               
               
def oracle_execute(connection, sql, values=None):
    '''run and commit some sql'''
    cur =connection.cursor()
    if values:
        cur.execute(sql, values)
    else:
        cur.execute(sql)
    connection.commit()
    cur.close()
 
       
def oracle_executemany(connection, sql, values):
    '''run and commit some sql'''
    cur =connection.cursor()
    cur.executemany(sql, values)
    connection.commit()
    cur.close()

        
def dict_to_oracle(cn, mydict, table):
    cols = ','.join(mydict.keys())
    colsyms = ','.join([':'+k for k in mydict.keys()])
    isql = """insert /*append*/ into TABLE (COLS) values (SYMS)""".replace('TABLE',table).replace('COLS',cols).replace('SYMS',colsyms)           
    oracle_execute(cn, isql, mydict.values())
        

### flight attribute reporting
def dump_flight_attributes(flight):
    '''print out full list of flight attributes'''
    for a in flight: 
        if type(a.value)==type(dict()):
            print a.name+':'
            for k,v in a.value.items():
                print '  '+k+':', v
        else:
            print a.name+':', a.value


def get_flight_record(source_file, output_path_and_file, registration, aircraft_info, flight, approach, kti, file_repository='central'):
    '''build a record-per-flight summary from the base analysis    '''
    flight_file = source_file
    base_file = os.path.basename(output_path_and_file)
    flt = OrderedDict([ ('file_repository',file_repository), ('source_file',flight_file), 
                        ('file_path', output_path_and_file), ('base_file_path', base_file), 
                        ('tail_number',registration), ('fleet_series', aircraft_info['Series']), ])    
                        
    attr = dict([(a.name, a.value) for a in flight])
    flt['operator']= 'xxx'
    flt['analyzer_version'] = attr.get('FDR Version','')
    flt['flight_type'] = attr.get('FDR Flight Type','')     
    flt['analysis_time'] = attr.get('FDR Analysis Datetime',None)
    
    lift = [k.index for k in kti if k.name=='Liftoff']
    flt['liftoff_min']        = min(lift) if len(lift)>0 else None
    tclimb = [k.index for k in kti if k.name=='Top of Climb']
    flt['top_of_climb_min']   = min(tclimb) if len(tclimb)>0 else None
    tdescent = [k.index for k in kti if k.name=='Top of Descent']
    flt['top_of_descent_min'] = min(tdescent) if len(tdescent)>0 else None
    tdown =[k.index for k in kti if k.name=='Touchdown']
    flt['touchdown_min']      = min(tdown) if len(tdown)>0 else None   
    flt['duration']       = attr.get('FDR Duration',None)

    if attr.get('FDR Takeoff Airport',None): #key must exist and contain a val other than None
        flt['orig_icao'] = attr['FDR Takeoff Airport']['code'].get('icao',None)
        flt['orig_iata'] = attr['FDR Takeoff Airport']['code'].get('iata',None)
        flt['orig_elevation'] = attr['FDR Takeoff Airport'].get('elevation',None)
    else:
        flt['orig_icao']=''; flt['orig_iata']=''; flt['orig_elevation']=None

    if attr.get('FDR Takeoff Runway',None):
        flt['orig_rwy'] = attr['FDR Takeoff Runway'].get('identifier',None)
        flt['orig_rwy_length'] = attr['FDR Takeoff Runway']['strip'].get('length',None)
    else:
        flt['orig_rwy']=''; flt['orig_rwy_length']=None
        
    if attr.get('FDR Landing Airport',None):
        flt['dest_icao'] = attr['FDR Landing Airport']['code'].get('icao',None)
        flt['dest_iata'] = attr['FDR Landing Airport']['code'].get('iata',None)
        flt['dest_elevation'] = attr['FDR Landing Airport'].get('elevation',None)
    else:
        flt['dest_icao']=''; flt['dest_iata']=''; flt['dest_elevation']=None

    if attr.get('FDR Landing Runway',None):
        flt['dest_rwy'] = attr['FDR Landing Runway'].get('identifier',None)
        flt['dest_rwy_length'] = attr['FDR Landing Runway']['strip'].get('length',None)
        if attr['FDR Landing Runway'].has_key('glideslope'):
            flt['glideslope_angle'] = attr['FDR Landing Runway']['glideslope'].get('angle',None)
        else:
            flt['glideslope_angle']=None
    else:
        flt['dest_rwy']=''; flt['dest_rwy_length']=None; flt['glideslope_angle']=None

    landing_count=0; go_around_count=0; touch_and_go_count=0
    for appr in approach:
        atype = appr.type
        if atype=='LANDING':        landing_count+=1
        elif atype=='GO_AROUND':    go_around_count+=1
        elif atype=='TOUCH_AND_GO': touch_and_go_count+=1
        else: pass
    flt['landing_count']        = landing_count
    flt['go_around_count']      = go_around_count
    flt['touch_and_go_count']   = touch_and_go_count

    flt['other_json'] = ''                  
    #dump_flight_attributes(res['flight'])
    return flt


def save_flight_record(cn, flight_record, OUTPUT_DIR, output_path_and_file):
     record_to_csv(flight_record.values(), OUTPUT_DIR+'flight_record.csv')
     repo = flight_record['file_repository']
     src = flight_record['source_file']
     dsql= """delete from fds_flight_record where file_repository='REPO' and source_file='SRC'""".replace('REPO',repo).replace('SRC', src)
     oracle_execute(cn, dsql)
     with hdfaccess.file.hdf_file(output_path_and_file) as hfile:
         flight_record['recorded_parameters'] = ','.join(hfile.lfl_keys())
     dict_to_oracle(cn, flight_record, 'fds_flight_record')
     logger.debug(flight_record)
     

### KPV KTI Phase reporting       
#     The values saved to Oracle and csv's are in seconds, making them convenient for reporting.
#     The values stored to pickle files maintain their original frequency and offset, for use with derive_parameters
def flight_measures_header():
    ''' KPV has value, KTI has lat/lon and datetime, phase has duration '''
    header = ['path', 'type', 'index', 'duration', 'name', 'value', 'datetime', 'latitude', 'longitude'] 
    return header

    
def initialize_flight_measures(OUTPUT_DIR, short_profile):
    measures_filename = OUTPUT_DIR + short_profile+'_measures.csv'  # KPV/KTI/Phase output
    if os.path.isfile(measures_filename):
        os.remove(measures_filename)
    with open(measures_filename, 'wt') as dest:
        dest.write(','.join(flight_measures_header())+'\n')
    return measures_filename    


def csv_flight_measures(hdf_path, kti_list, kpv_list, phase_list, dest_path):
    ''' send flight details to CSV'''
    #print 'csv flight measures', hdf_path, dest_path
    header = flight_measures_header()
    rows = flight_measures(hdf_path, kti_list, kpv_list, phase_list)     
    with open(dest_path, 'at') as dest:
        for row in rows:
            vals = [ str(row.get(col,'')) for col in header]
            dest.write( ','.join(vals) +'\n')
    return rows


def kti_to_oracle(cn, profile, flight_file, output_path_and_file, kti, file_repository='central'):
    '''node: index name datetime latitude longitude'''
    if profile=='base':
        base_file = os.path.basename(output_path_and_file)
    else:
        base_file = os.path.basename(flight_file)
        
    rows = []    
    for value in kti:
        vals = [profile, flight_file, value.name, float(value.index), base_file, file_repository]
        if value.index and value.index>=0:
            rows.append( vals )    
        else:
            print 'suspect kti index', value.name, value.index
    dsql= """delete from fds_kti where file_repository='REPO' and source_file='SRC' and profile='PROFILE'""".replace('PROFILE',profile).replace('REPO',file_repository).replace('SRC', flight_file)
    oracle_execute(cn, dsql)

    isql = """insert /*append*/ into fds_kti (profile, source_file,  name,  time_index, base_file_path, file_repository) 
                                    values (:profile, :source_file, :name, :time_index, :base_file_path, :file_repository)"""                
    oracle_executemany(cn, isql, rows)


def kpv_to_oracle(cn, profile, flight_file, output_path_and_file, params, kpv, file_repository='central'):
    '''node: index value name slice datetime latitude longitude'''
    if profile=='base':
        base_file = os.path.basename(output_path_and_file)
    else:
        base_file=os.path.basename(flight_file)
    
    rows = []    
    for value in kpv:
        try:
            units = params.get(value.name).units
        except:
            units = None
        vals = [profile, flight_file, value.name, float(value.index), float(value.value), base_file, units, file_repository ] 
        rows.append( vals )
    dsql= """delete from fds_kpv where file_repository='REPO' and source_file='SRC' and profile='PROFILE'""".replace('PROFILE',profile).replace('REPO',file_repository).replace('SRC', flight_file)
    oracle_execute(cn, dsql)
    isql = """insert /*append*/ into fds_kpv (profile, source_file,  name,  time_index,  value,  base_file_path,  units, file_repository) 
                                    values (:profile, :source_file, :name, :time_index, :value, :base_file_path, :units, :file_repository)"""
    oracle_executemany(cn, isql, rows)

    
def phase_to_oracle(cn, profile, flight_file, output_path_and_file, phase_list, file_repository='central'):
    '''node: 'name slice start_edge stop_edge'''
    if profile=='base':
        base_file = os.path.basename(output_path_and_file)
    else:
        base_file=os.path.basename(flight_file)
    rows = []    
    for value in phase_list:
        vals = [profile, flight_file, value.name, float(value.start_edge), float(value.stop_edge), value.stop_edge-value.start_edge, base_file, file_repository ]                
        rows.append( vals )
    dsql= """delete from fds_phase where file_repository='REPO' and source_file='SRC' and profile='PROFILE'""".replace('PROFILE',profile).replace('REPO',file_repository).replace('SRC', flight_file)
    oracle_execute(cn, dsql)
    isql = """insert /*append*/ into fds_phase (profile, source_file,  name,  time_index,  stop_edge, duration, base_file_path, file_repository) 
                                    values (:profile, :source_file, :name,   :time_index, :stop_edge, :duration, :base_file_path, :file_repository)"""
    oracle_executemany(cn, isql, rows)

             
def pkl_suffix():
    '''file suffix versioning'''
    return 'ver'+analyzer_version.replace('.','_') +'.pkl'  # eg 0.0.5 => ver0_0_5.pkl

def get_precomputed_parameters(flight_path_and_file, flight):    
    ''' if pkl file exists and matches version, in read it into params dict'''
    # suffix includes FDS version as a compatibility check
    source_file = flight_path_and_file.replace('.hdf5', pkl_suffix())
    precomputed_parameters={}
    if os.path.isfile(source_file):
        logger.info('get_precomputed_profiles. found: '+ source_file)
        with open(source_file, 'rb') as pkl_file:
            precomputed_parameters = pickle.load(pkl_file)
    else:
        logger.info('No compatible precomputed profile found')
    return precomputed_parameters


def flight_measures(hdf_path, kti_list, kpv_list, phase_list):
    '''Adapted from FDS FlightDataAnalyzer/plot_flight.py csv_flight_details()
        No HDF5 sourced values are included.'''
    rows = []
    
    for value in kti_list:
        vals = value.todict()  # recordtype
        vals['path'] = hdf_path
        vals['type'] = 'Key Time Instance'
        rows.append( vals )

    for value in kpv_list:
        vals = value.todict()  # recordtype
        vals['path'] = hdf_path
        vals['type'] = 'Key Point Value'
        rows.append( vals )

    for value in phase_list:
        vals = value._asdict()  # namedtuple
        vals['name'] = value.name
        vals['path'] = hdf_path
        vals['type'] = 'Phase'
        vals['index'] = value.start_edge
        vals['duration'] = value.stop_edge - value.start_edge  # (secs)
        rows.append(vals)

    rows = sorted(rows, key=lambda x: x['index'])
    return rows
    

def make_kml_file(start_datetime, flight_attrs, kti, kpv, flight_file, REPORTS_DIR, output_path_and_file): 
    '''adapted from FDS process_flight.  As of 2013/6/6 we do not geolocate unless KML was requested, to save time.'''
    from analysis_engine.plot_flight    import track_to_kml
    with hdf_file(output_path_and_file) as hdf:
        # geo locate KTIs
        kti = geo_locate(hdf, kti)
        kti = _timestamp(start_datetime, kti)                    
        # geo locate KPVs
        kpv = geo_locate(hdf, kpv)
        kpv = _timestamp(start_datetime, kpv)
    report_path_and_file = REPORTS_DIR + flight_file.replace('.','_')+'.kml'
    track_to_kml(output_path_and_file, kti, kpv, flight_attrs, dest_path=report_path_and_file)
    

### run FlightDataAnalyzer for analyze and profile
def prep_nodes(short_profile, module_names, include_flight_attributes):
    ''' go through modules to get derived nodes and check if we need to write a new hdf5 file'''
    if short_profile=='base':
        required_nodes  = get_derived_nodes(settings.NODE_MODULES + module_names)
        derived_nodes   = required_nodes        
        write_hdf = True
        required_params = required_nodes.keys()
        exclusions = ['Transmit', 
                      'EngGasTempDuringMaximumContinuousPowerForXMinMax',  #still calcs
                      'Eng Gas Temp During Maximum Continuous Power For X Min Max',
                      'EngGasTempDuringEngStartForXSecMax',
                      'Eng Gas Temp During Eng Start For X Sec Max',
                      ]
        required_params = sorted( set(required_params ) - set(exclusions)) #exclude select params from FDS set              
        if include_flight_attributes:
            required_params = list(set( required_params + get_derived_nodes(['analysis_engine.flight_attribute']).keys()))            
    else:
        required_nodes = get_derived_nodes(module_names)    
        derived_nodes  = get_derived_nodes(settings.NODE_MODULES + module_names)
        required_params = required_nodes.keys()
        # determine whether we'll need to copy the hdf5 to store new timeseries
        write_hdf = False
        for (name, nd) in required_nodes.items():
            if str(nd.__bases__).find('DerivedParameterNode')>=0: write_hdf=True    
    return required_params, derived_nodes, write_hdf
               

def prep_order(frame_dict, test_file, start_datetime, derived_nodes, required_params):
    ''' open example HDF to see recorded params and build process order'''
    _, _, _, registration = get_info_from_filename(os.path.basename(test_file), frame_dict)
    aircraft_info         = frame_dict[registration]

    with hdf_file(test_file) as hdf:
        # get list of all valid parameters: recorded or previously derived
        # Also, this ignores 'invalid' parameter attribute.  Unclear where that is set, and process_flight.derive_parameters() chks for it
        series_keys = hdf.valid_param_names()[:]
        check_duration = hdf.duration

    derived_nodes_copy = derived_nodes #copy.deepcopy(derived_nodes)
    series_copy = series_keys[:]
    node_mgr = NodeManager( start_datetime, check_duration, 
                            series_copy,       #from HDF.   was hdf.valid_param_names(), #hdf_keys; should be from LFL
                            required_params,   #requested
                            derived_nodes_copy,     #methods that can be computed; equals profile + base nodes   ????
                            aircraft_info, 
                            achieved_flight_record={'Myfile':test_file,'Mydict':dict()}
                            )
    # calculate dependency tree
    process_order, gr_st = dependency_order(node_mgr, draw=False)     
    logger.warning( 'process order: ' + str(process_order[:5]) + '...' ) #, gr_st
    return series_keys, process_order
    

def get_output_file(OUTPUT_DIR, flight_path_and_file, short_profile, write_hdf):
    ''' if no new timeseries, just set output path  input path'''
    if write_hdf:
        logger.debug('writing new hdf5')
        flight_file          = os.path.basename(flight_path_and_file)
        output_path_and_file = (OUTPUT_DIR+flight_file).replace('.0','_0').replace('.hdf5', '_'+short_profile+'.hdf5')
        shutil.copyfile(flight_path_and_file, output_path_and_file)  
    else:
        logger.debug('read only. no new hdf5')
        output_path_and_file = flight_path_and_file            
    return output_path_and_file 


class Flight(object):
    '''Container for data describing a single flight, principally from an hdf5 file
        used in conjunction with get_deps_series(), derive_parameters_series(), and derive()
        
       These support profile development.  Also make be useful for processing FFDs.
    '''
    def __init__(self):
        self.filepath = None
        self.duration = None
        self.start_datetime= None
        self.aircraft_info = {}            
        self.series = {}         #dict of ParameterNode         
        self.invalid_series = {} #dict of ParameterNode marked invalid
        self.lfl_params = []
        #maybe add params, kpv, phase etc. later
        
    def load_from_hdf5(self, flight_dict):
        '''load data from an hdf flight data file
            flight_series is a dictionary with fields:  'filepath', 'aircraft_info', 'repo'        
        '''
        # look up aircraft info by tail number
        self.filepath = flight_dict['filepath']
        self.aircraft_info  = flight_dict['aircraft_info']
        
        flight_file   = os.path.basename(self.filepath)    
        print flight_file
        
        with hdfaccess.file.hdf_file(self.filepath) as ff:
            self.duration = ff.duration
            self.start_datetime = ff.start_datetime
            # load valid hdf series into ParameterNode objects, invalid series into a separate dict
            for s in ff.keys():
                if ff[s].lfl==1:
                    self.lfl_params.append(s)
            for s in ff.keys():
                if ff[s].invalid and ff[s].invalid==True:
                    self.invalid_series[s] = node.derived_param_from_hdf(ff.get_param(s, valid_only=False))   
                else:
                    #print s, ff.get(s).units
                    param = node.derived_param_from_hdf(ff.get_param(s, valid_only=True))
                    param.units = ff.get(s).units
                    self.series[s] = param
    
    def __repr__(self):
        s='class Flight'
        s = s+ '\n  filepath:       ' + str(self.filepath)
        s = s+ '\n  duration:       ' + str(self.duration)
        s = s+ '\n  start_datetime: ' + str(self.start_datetime)
        s = s+ '\n  series:         ' + str(self.series.keys()[:3]) + '...'
        s = s+ '\n  invalid_series: ' + str(self.invalid_series.keys()[:3]) + '...'
        s = s+ '\n  aircraft_info:  ' + str(self.aircraft_info)
        return s


def get_deps_series(node_class, params, node_mgr, series):
        # build ordered dependencies without touching hdf file
        # 'params' is a dictionary of previously computed nodes
        deps = []
        node_deps = node_class.get_dependency_names()
        #if DEBUG: print '  dependencies: ', node_deps
        for dep_name in node_deps:
            #print dep_name, (dep_name in node_mgr.hdf_keys)
            if dep_name in params:  # already calculated KPV/KTI/Phase
                deps.append(params[dep_name])
            elif node_mgr.get_attribute(dep_name) is not None:
                deps.append(node_mgr.get_attribute(dep_name))
            elif dep_name in series.keys():
                deps.append(series[dep_name])
            else:  # dependency not available
                deps.append(None)
        if all([d is None for d in deps]):
            print deps
            raise RuntimeError("No dependencies available - Nodes cannot "
                               "operate without ANY dependencies available! "
                               "Node: %s" % node_class.__name__)
        return deps


def get_deps(node_class, params, node_mgr, series, h5flight):
        # build ordered dependencies
        # 'params' is a dictionary of previously computed nodes
        deps = []
        node_deps = node_class.get_dependency_names()
        #if DEBUG: print '  dependencies: ', node_deps
        for dep_name in node_deps:
            #print dep_name, (dep_name in node_mgr.hdf_keys)
            if dep_name in params:  # already calculated KPV/KTI/Phase
                deps.append(params[dep_name])
            elif node_mgr.get_attribute(dep_name) is not None:
                deps.append(node_mgr.get_attribute(dep_name))
            elif dep_name in node_mgr.hdf_keys:  
                # LFL/Derived parameter
                # all parameters (LFL or other) need get_aligned which is
                # available on DerivedParameterNode
                try:
                    #dp = series.get(dep_name)  ##########################################                    
                    dp = derived_param_from_hdf(h5flight.get_param(dep_name, valid_only=True))
                    print 'derived hdf dep', dep_name
                except KeyError:
                    # Parameter is invalid.
                    print 'key error hdf dep', dep_name
                    dp = None
                deps.append(dp)
            elif dep_name in series.keys():
                deps.append(series[dep_name])
            else:  # dependency not available
                deps.append(None)
        if all([d is None for d in deps]):
            print deps
            raise RuntimeError("No dependencies available - Nodes cannot "
                               "operate without ANY dependencies available! "
                               "Node: %s" % node_class.__name__)
        return deps
        

###node post-processing
def align_section(result, duration, section_list):
    aligned_section = result.get_aligned(P(frequency=1, offset=0))
    for index, one_hz in enumerate(aligned_section):
        # SectionNodes allow slice starts and stops being None which
        # signifies the beginning and end of the data. To avoid TypeErrors
        # in subsequent derive methods which perform arithmetic on section
        # slice start and stops, replace with 0 or hdf.duration.
        fallback = lambda x, y: x if x is not None else y
        duration = fallback(duration, 0)

        start = fallback(one_hz.slice.start, 0)
        stop = fallback(one_hz.slice.stop, duration)
        start_edge = fallback(one_hz.start_edge, 0)
        stop_edge = fallback(one_hz.stop_edge, duration)
        slice_ = slice(start, stop)
        one_hz = Section(one_hz.name, slice_, start_edge, stop_edge)
        aligned_section[index] = one_hz
        
        if not (0 <= start <= duration and 0 <= stop <= duration + 1):
            msg = "Section '%s' (%.2f, %.2f) not between 0 and %d"
            raise IndexError(msg % (one_hz.name, start, stop, duration))
        if not 0 <= start_edge <= duration:
            msg = "Section '%s' start_edge (%.2f) not between 0 and %d"
            raise IndexError(msg % (one_hz.name, start_edge, duration))
        if not 0 <= stop_edge <= duration + 1:
            msg = "Section '%s' stop_edge (%.2f) not between 0 and %d"
            raise IndexError(msg % (one_hz.name, stop_edge, duration))
        section_list.append(one_hz)
    return aligned_section, section_list
            
            
def check_approach(result, duration, approach_list):
    aligned_approach = result.get_aligned(P(frequency=1, offset=0))
    for approach in aligned_approach:
        # Does not allow slice start or stops to be None.
        valid_turnoff = (not approach.turnoff or
                         (0 <= approach.turnoff <= duration))
        valid_slice = ((0 <= approach.slice.start <= duration) and
                       (0 <= approach.slice.stop <= duration))
        valid_gs_est = (not approach.gs_est or
                        ((0 <= approach.gs_est.start <= duration) and
                         (0 <= approach.gs_est.stop <= duration)))
        valid_loc_est = (not approach.loc_est or
                         ((0 <= approach.loc_est.start <= duration) and
                          (0 <= approach.loc_est.stop <= duration)))
        if not all([valid_turnoff, valid_slice, valid_gs_est,
                    valid_loc_est]):
            raise ValueError('ApproachItem contains index outside of '
                             'flight data: %s' % approach)
    return aligned_approach, approach_list
            

def check_derived_array(param_name, result, duration ):
    # check that the right number of results were returned
    # Allow a small tolerance. For example if duration in seconds
    # is 2822, then there will be an array length of  1411 at 0.5Hz and 706
    # at 0.25Hz (rounded upwards). If we combine two 0.25Hz
    # parameters then we will have an array length of 1412.
    expected_length = duration * result.frequency
    if result.array is None:
        logger.debug("No array set; creating a fully masked array for %s", param_name)
        array_length = expected_length
        # Where a parameter is wholly masked, we fill the HDF
        # file with masked zeros to maintain structure.
        result.array = \
            np_ma_masked_zeros_like(np.ma.arange(expected_length))
    else:
        array_length = len(result.array)
    length_diff = array_length - expected_length
    if length_diff == 0:
        pass
    elif 0 < length_diff < 5:
        logger.debug("Cutting excess data for parameter '%s'. Expected length was '%s' while resulting "
                       "array length was '%s'.", param_name, expected_length, len(result.array))
        result.array = result.array[:expected_length]
    else:
        #pdb.set_trace()
        raise ValueError("Array length mismatch for parameter "
                         "'%s'. Expected '%s', resulting array length '%s'." % (param_name, expected_length, array_length))
    return result
            

def derive_parameters_series(flight, node_mgr, process_order, precomputed={}):
    '''
    Non HDF5 version. Suitable for FFD and Notebook profile development.
    
    Derives the parameter values and if limits are available, applies
    parameter validation upon each param before storing the resulting masked
    array back into the hdf file.
    
    :param series: Data file accessor used to get and save parameter data and attributes
    :type series: dictionary of ParameterNode objects
    :param node_mgr: Used to determine the type of node in the process_order
    :type node_mgr: NodeManager
    :param process_order: Parameter / Node class names in the required order to be processed
    :type process_order: list of strings
    '''
    params    = OrderedDict(precomputed) #{}   # dictionary of derived params that aren't masked arrays
    res     = {'series':{}, 
              'approach': ApproachNode(restrict_names=False),
              'kpv': KeyPointValueNode(restrict_names=False),
              'kti': KeyTimeInstanceNode(restrict_names=False),
              'phase': SectionNode(),
              'attr': []
              } #results by node type
   
    for param_name in process_order:
        if param_name in node_mgr.hdf_keys:
            logger.info('_derive_: hdf '+param_name)            
            continue        
        elif node_mgr.get_attribute(param_name) is not None:
            logger.info('_derive_: get_attribute '+param_name)
            continue
        elif param_name in params.keys():  # already calculated KPV/KTI/Phase ***********************NEW
            logger.info('_derive_: re-using precomputed'+param_name)
            continue
        elif param_name in res['series'].keys():
            logger.info('_derive_: re-using derived'+param_name)
            continue


        ####compute###########################################################    
        logger.debug('_derive_: computing '+param_name)        
        node_class = node_mgr.derived_nodes[param_name]  #NB raises KeyError if Node is "unknown"
        deps = get_deps_series(node_class, params, node_mgr, flight.series )  
        node = node_class()
        # Derive the resulting value
        if param_name =="Configuration":
            print deps
        result = node.get_derived(deps)
        ###############################################################

        #post-process (node, result, params, res)
        if node.node_type is KeyPointValueNode:
            #Q: track node instead of result here??
            params[param_name] = result
            for one_hz in result.get_aligned(P(frequency=1, offset=0)):
                if not (0 <= one_hz.index <= flight.duration):
                    raise IndexError("KPV '%s' index %.2f is not between 0 and %d" % (one_hz.name, one_hz.index, flight.duration))
                res['kpv'].append(one_hz)
        
        elif node.node_type is KeyTimeInstanceNode:
            params[param_name] = result
            for one_hz in result.get_aligned(P(frequency=1, offset=0)):
                if not (0 <= one_hz.index <= flight.duration):
                    raise IndexError("KTI '%s' index %.2f is not between 0 and %d" % (one_hz.name, one_hz.index, flight.duration))
                res['kti'].append(one_hz)
        
        elif node.node_type is FlightAttributeNode:
            params[param_name] = result
            try:
                res['attr'].append(Attribute(result.name, result.value)) # only has one Attribute result
            except:
                logger.warning("Flight Attribute Node '%s' returned empty handed.", param_name)
                    
        elif issubclass(node.node_type, SectionNode):
            aligned_section, res['phase'] = align_section(result, flight.duration, res['phase'])
            params[param_name] = aligned_section  ### 
            
        elif issubclass(node.node_type, DerivedParameterNode):
            logger.info('series: ' + param_name)
            result = check_derived_array(param_name, result, flight.duration)
            #print str(result
            res['series'][param_name] = result  
            res['series'][param_name].name = param_name
            params[param_name] = result
            
        elif issubclass(node.node_type, ApproachNode):
            aligned_approach, res['approach'] = check_approach(result, flight.duration, res['approach'])   
            params[param_name] = aligned_approach
        else:
            raise NotImplementedError("Unknown Type %s" % node.__class__)
        continue
    return res, params


def derive_parameters_mitre(hdf, node_mgr, process_order, precomputed_parameters={}):
    '''
    Derives the parameter values and if limits are available, applies
    parameter validation upon each param before storing the resulting masked
    array back into the hdf file.
    
    :param hdf: Data file accessor used to get and save parameter data and attributes
    :type hdf: hdf_file
    :param node_mgr: Used to determine the type of node in the process_order
    :type node_mgr: NodeManager
    :param process_order: Parameter / Node class names in the required order to be processed
    :type process_order: list of strings
    '''
    params    = precomputed_parameters   # dictionary of derived params that aren't masked arrays

    approach_list = ApproachNode(restrict_names=False)
    kpv_list = KeyPointValueNode(restrict_names=False) # duplicate storage, but maintaining types
    kti_list = KeyTimeInstanceNode(restrict_names=False)
    section_list = SectionNode()  # 'Node Name' : node()  pass in node.get_accessor()
    flight_attrs = []
    duration = hdf.duration
    
    for param_name in process_order:
        if param_name in node_mgr.hdf_keys:
            logger.debug('  derive_: hdf '+param_name)            
            continue        
        elif node_mgr.get_attribute(param_name) is not None:
            logger.debug('  derive_: get_attribute '+param_name)
            continue
        elif param_name in params.keys():  # already calculated KPV/KTI/Phase ***********************NEW
            logger.debug('  derive_parameters: re-using '+param_name)
            continue

        logger.debug('  derive_: computing '+param_name)        
        node_class = node_mgr.derived_nodes[param_name]  #NB raises KeyError if Node is "unknown"        
        deps = get_deps(node_class, params, node_mgr, hdf)
        
        # initialise node
        node = node_class()
        logger.info("Processing parameter %s", param_name)
        # Derive the resulting value

        result = node.get_derived(deps)

        if node.node_type is KeyPointValueNode:
            #Q: track node instead of result here??
            params[param_name] = result
            for one_hz in result.get_aligned(P(frequency=1, offset=0)):
                if not (0 <= one_hz.index <= duration):
                    raise IndexError(
                        "KPV '%s' index %.2f is not between 0 and %d" %
                        (one_hz.name, one_hz.index, duration))
                kpv_list.append(one_hz)
        elif node.node_type is KeyTimeInstanceNode:
            params[param_name] = result
            for one_hz in result.get_aligned(P(frequency=1, offset=0)):
                if not (0 <= one_hz.index <= duration):
                    raise IndexError(
                        "KTI '%s' index %.2f is not between 0 and %d" %
                        (one_hz.name, one_hz.index, duration))
                kti_list.append(one_hz)
        elif node.node_type is FlightAttributeNode:
            params[param_name] = result
            try:
                flight_attrs.append(Attribute(result.name, result.value)) # only has one Attribute result
            except:
                logger.warning("Flight Attribute Node '%s' returned empty "
                               "handed.", param_name)
        elif issubclass(node.node_type, SectionNode):
            aligned_section = result.get_aligned(P(frequency=1, offset=0))
            for index, one_hz in enumerate(aligned_section):
                # SectionNodes allow slice starts and stops being None which
                # signifies the beginning and end of the data. To avoid TypeErrors
                # in subsequent derive methods which perform arithmetic on section
                # slice start and stops, replace with 0 or hdf.duration.
                fallback = lambda x, y: x if x is not None else y

                duration = fallback(duration, 0)

                start = fallback(one_hz.slice.start, 0)
                stop = fallback(one_hz.slice.stop, duration)
                start_edge = fallback(one_hz.start_edge, 0)
                stop_edge = fallback(one_hz.stop_edge, duration)

                slice_ = slice(start, stop)
                one_hz = Section(one_hz.name, slice_, start_edge, stop_edge)
                aligned_section[index] = one_hz
                
                if not (0 <= start <= duration and 0 <= stop <= duration + 1):
                    msg = "Section '%s' (%.2f, %.2f) not between 0 and %d"
                    raise IndexError(msg % (one_hz.name, start, stop, duration))
                if not 0 <= start_edge <= duration:
                    msg = "Section '%s' start_edge (%.2f) not between 0 and %d"
                    raise IndexError(msg % (one_hz.name, start_edge, duration))
                if not 0 <= stop_edge <= duration + 1:
                    msg = "Section '%s' stop_edge (%.2f) not between 0 and %d"
                    raise IndexError(msg % (one_hz.name, stop_edge, duration))
                section_list.append(one_hz)
            params[param_name] = aligned_section
        elif issubclass(node.node_type, DerivedParameterNode):
            if duration:
                # check that the right number of results were returned
                # Allow a small tolerance. For example if duration in seconds
                # is 2822, then there will be an array length of  1411 at 0.5Hz and 706
                # at 0.25Hz (rounded upwards). If we combine two 0.25Hz
                # parameters then we will have an array length of 1412.
                expected_length = duration * result.frequency
                if result.array is None:
                    logger.warning("No array set; creating a fully masked array for %s", param_name)
                    array_length = expected_length
                    # Where a parameter is wholly masked, we fill the HDF
                    # file with masked zeros to maintain structure.
                    result.array = \
                        np_ma_masked_zeros_like(np.ma.arange(expected_length))
                else:
                    array_length = len(result.array)
                length_diff = array_length - expected_length
                if length_diff == 0:
                    pass
                elif 0 < length_diff < 5:
                    logger.warning("Cutting excess data for parameter '%s'. "
                                   "Expected length was '%s' while resulting "
                                   "array length was '%s'.", param_name,
                                   expected_length, len(result.array))
                    result.array = result.array[:expected_length]
                else:
                    pdb.set_trace()
                    raise ValueError("Array length mismatch for parameter "
                                     "'%s'. Expected '%s', resulting array "
                                     "length '%s'." % (param_name,
                                                       expected_length,
                                                       array_length))
            
            hdf.set_param(result)
            # Keep hdf_keys up to date.
            node_mgr.hdf_keys.append(param_name)
        elif issubclass(node.node_type, ApproachNode):
            aligned_approach = result.get_aligned(P(frequency=1, offset=0))
            for approach in aligned_approach:
                # Does not allow slice start or stops to be None.
                valid_turnoff = (not approach.turnoff or
                                 (0 <= approach.turnoff <= duration))
                valid_slice = ((0 <= approach.slice.start <= duration) and
                               (0 <= approach.slice.stop <= duration))
                valid_gs_est = (not approach.gs_est or
                                ((0 <= approach.gs_est.start <= duration) and
                                 (0 <= approach.gs_est.stop <= duration)))
                valid_loc_est = (not approach.loc_est or
                                 ((0 <= approach.loc_est.start <= duration) and
                                  (0 <= approach.loc_est.stop <= duration)))
                if not all([valid_turnoff, valid_slice, valid_gs_est,
                            valid_loc_est]):
                    raise ValueError('ApproachItem contains index outside of '
                                     'flight data: %s' % approach)
                approach_list.append(approach)
            params[param_name] = aligned_approach
        else:
            raise NotImplementedError("Unknown Type %s" % node.__class__)
        continue
    return kti_list, kpv_list, section_list, approach_list, flight_attrs, params


def dump_pickles(output_path_and_file, params, kti, kpv, phases, approach, flight_attrs, logger):
    #dump params to pickle file -- versioned
    pkl_end = pkl_suffix()
    pickle_file=output_path_and_file.replace('.hdf5',pkl_end )
    with open(pickle_file, 'wb') as output:
        pickle.dump(params, output)
    with open( pickle_file.replace(pkl_end,'_kti_'+pkl_end), 'wb') as output:
        pickle.dump(kti, output)
    with open(pickle_file.replace(pkl_end,'_kpv_'+pkl_end), 'wb') as output:
        pickle.dump(kpv, output)
    with open(pickle_file.replace(pkl_end,'_phases_'+pkl_end), 'wb') as output:
        pickle.dump(phases, output)
    with open(pickle_file.replace(pkl_end,'_approach_'+pkl_end), 'wb') as output:
        pickle.dump(approach, output)
    with open(pickle_file.replace(pkl_end,'_fltattr_'+pkl_end), 'wb') as output:
        pickle.dump(flight_attrs, output)
    logger.info('saved '+ pickle_file)


###################################################################################################

        
        
def run_analyzer(short_profile,    module_names,
                 logger,           files_to_process, 
                 input_dir,        output_dir,       reports_dir, 
                 include_flight_attributes=False, 
                 make_kml=False,   save_oracle=True, comment='',
                 file_repository='central'):    
    '''
    run FlightDataAnalyzer for analyze and profile. mostly file mgmt and reporting.
    currently runs against a single fleet at a time.
    '''        
    if not files_to_process or len(files_to_process)==0:
        print 'run_analyzer: No files to process.'
        return
    input_dir = input_dir if input_dir.endswith('/') or input_dir.endswith('\\') else input_dir+'/'
    output_dir = output_dir if output_dir.endswith('/') or output_dir .endswith('\\') else output_dir +'/'
    reports_dir = reports_dir if reports_dir.endswith('/') or reports_dir.endswith('\\') else reports_dir +'/'

    timestamp      = datetime.now()
    start_datetime = datetime(2012, 4, 1, 0, 0, 0)
    frame_dict     = frame_list.build_frame_list(logger)            
    cn = fds_oracle.get_connection() if save_oracle else None
    
    # set up dependencies outside loop  
    required_params, derived_nodes, write_hdf = prep_nodes(short_profile, module_names, include_flight_attributes)
    test_file  = files_to_process[0]
    logger.warning( 'test_file for prep_order(): '+ test_file)
    series_keys, process_order = prep_order(frame_dict, test_file, start_datetime, derived_nodes, required_params)
    
    file_count = len(files_to_process)
    logger.warning( 'Processing '+str(file_count)+' files.')
    start_time = time.time()
    ### loop over files        
    for flight_path_and_file in files_to_process:
        file_start_time = time.time()
        flight_file          = os.path.basename(flight_path_and_file)
        logger.debug('starting '+ flight_file)
        output_path_and_file  = get_output_file(output_dir, flight_path_and_file, short_profile, write_hdf)

        _, _, _, registration = get_info_from_filename(flight_file, frame_dict)
        aircraft_info         = frame_dict[registration]
        aircraft_info['Tail Number'] = registration
        logger.debug(aircraft_info)
        logger.debug(' *** Processing flight %s', flight_file)
        #try: 
        if True:
            derived_nodes_copy = copy.deepcopy(derived_nodes)
            series_copy = series_keys[:]
            with hdf_file(output_path_and_file) as hdf:
                node_mgr = NodeManager( start_datetime, hdf.duration, 
                                        series_copy,  #hdf.valid_param_names(),
                                        required_params, derived_nodes_copy, aircraft_info,
                                        achieved_flight_record={'Myfile':output_path_and_file, 'Mydict':dict()}
                                        )
                precomputed_parameters={} if short_profile=='base' else get_precomputed_parameters(flight_path_and_file, node_mgr)
                kti, kpv, phases, approach, flight_attrs, params = derive_parameters_mitre(hdf, node_mgr, process_order, precomputed_parameters)                
 
            if short_profile=='base': dump_pickles(output_path_and_file, params, kti, kpv, phases, approach, flight_attrs, logger)
            status='ok'
        '''
        except:
            ex_type, ex, tracebck = sys.exc_info()
            logger.warning('ANALYZER ERROR '+flight_file)
            traceback.print_tb(tracebck)
            status='failed'
            del tracebck                
        '''    
        logger.info(' *** Processing flight %s finished ' + flight_file + ' time: ' + str(time.time()-file_start_time) + 'status: '+status)
        # reports
        stage = 'analyze' if short_profile=='base' else 'profile'    
        processing_time = time.time()-file_start_time
        report_timing(timestamp, stage, short_profile, flight_path_and_file, processing_time, status, logger, cn)

        if save_oracle and status=='ok':
            kti_to_oracle(cn, short_profile, flight_file, output_path_and_file, kti, file_repository)
            phase_to_oracle(cn, short_profile, flight_file, output_path_and_file, phases, file_repository)
            kpv_to_oracle(cn, short_profile, flight_file, output_path_and_file, params, kpv, file_repository)
            if short_profile=='base':  # for base analyze, store flight record
                 flight_record = get_flight_record(flight_file, output_path_and_file, registration, aircraft_info, flight_attrs, approach, kti, file_repository) # an OrderedDict
                 save_flight_record(cn, flight_record, output_dir, output_path_and_file)                     
            logger.debug('done ora out')
        if status=='ok' and make_kml:
            make_kml_file(start_datetime, flight_attrs, kti, kpv, flight_file, reports_dir, output_path_and_file)

    ### end loop
    report_job(timestamp, stage, short_profile, comment, input_dir, output_dir, 
               len(files_to_process), (time.time()-start_time), logger, db_connection=cn)
    if save_oracle:  cn.close()
    for handler in logger.handlers: handler.close()        
    return aircraft_info
    

def run_profile(profile_name, module_names, LOG_LEVEL, FILES_TO_PROCESS, COMMENT, MAKE_KML_FILES, 
                FILE_REPOSITORY='central', save_oracle=True ):
    reports_dir = settings.PROFILE_REPORTS_PATH
    logger = initialize_logger(LOG_LEVEL)
    # Determine module names so FlightDataAnalyzer knows what nodes it is working with. Must be in PYTHON_PATH.
    #  Normally we are just passing the current profile, but we could also send a list of profiles.
    logger.warning('profile: '+profile_name)
    output_dir = settings.PROFILE_DATA_PATH + profile_name+'/' 
    if not os.path.exists(output_dir): os.makedirs(output_dir)

    run_analyzer(profile_name, module_names, 
             logger, FILES_TO_PROCESS, 
             'NA', output_dir, reports_dir, 
             include_flight_attributes=False, 
             make_kml=MAKE_KML_FILES, 
             save_oracle=save_oracle,
             comment=COMMENT,
             file_repository=FILE_REPOSITORY)   
