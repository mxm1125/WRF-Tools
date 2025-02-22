#!/usr/bin/python

'''
Created on 2012-03-20
Revised on 2013-03-19

Script to prepare input data from various sources (including CESM/CCSM) and run the 
WPS/metgrid.exe tool chain, in order to generate input data for WRF/real.exe

@author: Andre R. Erler
'''

##  imports
import os # directory operations
import shutil # copy and move
import re # regular expressions
import subprocess # launching external programs
import multiprocessing # parallelization
import string # to iterate over alphabet...
import datetime as dt  #datelist construction
# my modules
import wrfrun.namelist_time as nlt
# module to call cdb_query
from wrfrun.call_cdb_query import apply_cdb_query_singleWPSstep

##  Default Settings (may be overwritten by in meta/namelist.py)
Alphabet = string.ascii_uppercase
tmp = 'tmp/'
meta = 'meta/'
# metgrid (this is all hard coded into metgrid)
nmlform = '{:04d}-{:02d}-{:02d}_{:02d}:00:00' # date in namelist.wps
imform = '{:04d}-{:02d}-{:02d}_{:02d}' # date in IM filename
impfx = 'FILE:'
metpfx = 'met_em.d{:02d}.'
metsfx = ':00:00.nc'
geopfx = 'geo_em.d{:02d}'
# parallelization
pname = 'proc{:02d}'
pdir = 'proc{:02d}/'
# destination folder(s)
ramlnk = 'ram' # automatically generated link to ramdisk (if applicable)
data = 'data/' # data folder in ram
ldata = True # whether or not to keep data in memory; can be set with environment variables
disk = 'data/' # destination folder on hard disk
Disk = '' # default: Root + disk
ldisk = False # don't write metgrid files to hard disk; can be set with environment variables
## Commands
# metgrid
metgrid_exe = 'metgrid.exe'
metgrid_log = 'metgrid.exe.log'
nmlstwps = 'namelist.wps'
ncext = '.nc' # also used for geogrid files
# dependent variables
METGRID = './' + metgrid_exe

# CMIP5 specific file names
validate_file = 'CMIP5data.validate.nc'
grid_file_orog = 'orog_file.nc'
grid_file_sftlf = 'sftlf_file.nc'
weight_file = 'ocn2atmweight_file.nc'


## determine which machine we are on
#import socket # recognizing host
#hostname = socket.gethostname()

## RAM-disk on a local workstation:
# use this command to mount: sudo mount -t ramfs -o size=100m ramfs $RAMDISK
# followed by: sudo chown $USER $RAMDISK
# and this to unmount:   sudo umount $RAMDISK
# e.g.: sudo mount -t ramfs -o size=100m ramfs /media/tmp/ && sudo chown $USER /media/tmp/

## read environment variables (overrides defaults)
# defaults are set above (some machine specific)
# code root folder (instalation folder of 'WRF Tools'
if 'CODE_ROOT' in os.environ: Model = os.environ['CODE_ROOT']
else: raise ValueError('Environment variable $CODE_ROOT not defined')
# NCARG installation folder (for NCL)
if 'NCARG_ROOT' in os.environ: 
  NCARG = os.environ['NCARG_ROOT']
  if NCARG[-1] != '/': NCARG += '/' # local convention is that directories already have a slash
  NCL = NCARG + 'bin/ncl'
# In case PYWPS_RAMDISK is defined by the caller (e.g. execWPS.sh)
if 'PYWPS_RAMDISK' in os.environ:
  lram = bool(int(os.environ['PYWPS_RAMDISK'])) 
else: lram = True
# RAM disk
if lram:
  if 'RAMDISK' in os.environ: 
    Ram = os.environ['RAMDISK']
  else: 
    raise ValueError('Error: Need to define RAMDISK.')
else:
  Ram = None
# keep data in memory (for real.exe)
if 'PYWPS_KEEP_DATA' in os.environ:
  ldata = bool(int(os.environ['PYWPS_KEEP_DATA'])) # expects 0 or 1
else: ldata = True
# discover data based on available files
if 'PYWPS_DISCOVER' in os.environ:
  ldiscover = bool(int(os.environ['PYWPS_DISCOVER'])) # expects 0 or 1
else: ldiscover = False
# save metgrid data
if 'PYWPS_MET_DATA' in os.environ and os.environ['PYWPS_MET_DATA']:
  Disk = os.environ['PYWPS_MET_DATA']
  if Disk[-1] != '/': Disk += '/' # local convention is that directories already have a slash
  ldisk = True
else: ldisk = False
# number of processes NP 
if 'PYWPS_THREADS' in os.environ: NP = int(os.environ['PYWPS_THREADS'])
# dataset specific stuff
if 'PYWPS_DATA_TYPE' in os.environ: 
  dataset = os.environ['PYWPS_DATA_TYPE']
else: 
  raise ValueError('Unknown dataset type ($PYWPS_DATA_TYPE not defined)')


## dataset manager parent class
class Dataset():
  # a class that encapsulates meta data and operations specific to certain datasets
  # note that this class does not hold any actual data 
  prefix = '' # reanalysis generally doesn't have a prefix'
  # ungrib
  vtable = 'Vtable' # this is hard coded into ungrib 
  gribname = 'GRIBFILE' # ungrib input filename trunk (needs extension, e.g. .AAA)
  ungrib_exe = 'ungrib.exe'
  ungrib_log = 'ungrib.exe.log'
  ungribout = 'FILE:{:04d}-{:02d}-{:02d}_{:02d}' # YYYY-MM-DD_HH ungrib.exe output format
  # meta data defaults
  grbdirs = None # list of source folders; same order as strings; has to be defined in child
  grbstrs = None # list of source files; filename including date string; has to be defined in child
  dateform = '\d\d\d\d\d\d\d\d\d\d' # YYYYMMDDHHMM (for matching regex)
  datestr = '{:04d}{:02d}{:02d}{:02d}' # year, month, day, hour (for printing)
  interval = 6 # data interval in hours (6-hourly data is most common)
  
  ## these functions are dataset specific and may have to be implemented in the child class
  def __init__(self, folder=None):
    # type checking
    if not isinstance(self.grbdirs,(list,tuple)): raise TypeError('Need to define a list of grib folders.')
    if not isinstance(self.grbstrs,(list,tuple)): raise TypeError('Need to define a list of grib file names.')
    if len(self.grbstrs) != len(self.grbdirs): raise ValueError('Grid file types and folders need to be of the same number.')
    if len(self.grbstrs) > len(Alphabet): raise ValueError('Currently only {0:d} file types are supported.'.format(len(Alphabet)))
    # some general assignments
    # N.B.: self.MainDir and self.mainrgx need to be assigned as well!
    # files and folders
    if not isinstance(folder,str): raise IOError('Warning: need to specify root folder!')
    self.folder = folder # needs to be set externally for different applications
    self.GrbDirs = ['{0:s}/{1:s}'.format(folder,grbdir) for grbdir in self.grbdirs]
    self.UNGRIB = './' + self.ungrib_exe
    # generate required ungrib names
    gribnames = []
    for i in range(len(self.grbstrs)):
      gribname = '{0:s}.AA{1:s}'.format(self.gribname,Alphabet[i])
      gribnames.append(gribname)
    self.gribnames = gribnames
    # regex to extract dates from filenames
    self.dateregx = re.compile(self.dateform)
    # master file list (first element in grib file list)
    self.MainDir = os.readlink(self.GrbDirs[0]) # directory to be searched for dates
    self.mainfiles = self.grbstrs[0].format(self.dateform) # regex definition for master list
    self.mainrgx = re.compile(self.mainfiles+'$') # use as master list    
  
  ## these functions will be very similar for all datasets using ungrib.exe (overload when not using ungrib.exe)
  def setup(self, src, dst, lsymlink=False):
    # method to copy dataset specific files and folders working directory
    # executables
    if lsymlink:
      cwd = os.getcwd()
      os.chdir(dst)
      # use current directory
      os.symlink(src+self.ungrib_exe, self.ungrib_exe)
      os.symlink(Meta+self.vtable,self.vtable) # link VTable
      os.chdir(cwd)
    else:
      shutil.copy(src+self.ungrib_exe, dst)
      shutil.copy(Meta+self.vtable, dst)
  
  def cleanup(self, tgt):
    # method to remove dataset specific files and links
    cwd = os.getcwd()
    os.chdir(tgt)
    # use current directory
    os.remove(self.ungrib_exe)
    os.remove(self.vtable)
    os.chdir(cwd)
  
  def extractDate(self, filename):
    ''' method to generate date tuple from date string in filename '''
    # match valid filenames
    match = self.mainrgx.match(filename) # return match object
    if match is None:
      return None # if the filename doesn't match the regex
    else:
      # extract date string
      datestr = self.dateregx.search(filename).group()
      # split date string into tuple
      year = int(datestr[0:4])
      month = int(datestr[4:6])
      day = int(datestr[6:8])
      hour = int(datestr[8:10])
      return (year, month, day, hour)

  def constructDateList(self, start, end):    
    ''' construct a list of dates where data should be available '''
#     # Pandas implementation
#     import pandas as pd
#     dates = pd.date_range(dt.datetime(*start), 
#                           dt.datetime(*end), 
#                           freq='{:d}H'.format(dataset.interval)) # generate datelist
#     # convert to the year-month-day-hour tuple
#     dates = [(date.year, date.month, date.day, date.hour) for date in dates]
    # datetime implementation
    curd = dt.datetime(*start); endd = dt.datetime(*end) # datetime objects
    delta = dt.timedelta(hours=self.interval) # usually an integer in hours...
    dates = [] # create date list
    while curd <= endd:
      dates.append((curd.year, curd.month, curd.day, curd.hour)) # format: year, month, days, hours
      curd += delta # increment date by interval
    # return properly formated list
    return dates

  def checkSubDir(self, *args):
    # method to determine whether data is stored in subfolders and can be processed recursively
    # most datasets will not have subfolders, we skip all subfolders by default    
    return False

  def ungrib(self, date, mytag):
    # method that generates the WRF IM file for metgrid.exe
    # create formatted date string
    datestr = self.datestr.format(*date) # (years, months, days, hours)
    msg = datestr # status output; message printed later
    # create links to relevant source data (requires full path for linked files)
    Grbfiles = [] # list of relevant source files
    for GrbDir,grbstr in zip(self.GrbDirs,self.grbstrs):
      grbfile = grbstr.format(datestr) # insert current date
      Grbfile = '{0:s}/{1:s}'.format(GrbDir,grbfile) # absolute path
      if not os.path.exists(Grbfile): 
        raise IOError("Input file '{0:s}' not found!".format(Grbfile))     
      else:
        msg += '\n    '+grbfile # add to output
        Grbfiles.append(Grbfile) # append to file list
    # print feedback
    print(('\n '+mytag+' Processing time-step:  '+msg))    
    for Gribfile,gribname in zip(Grbfiles,self.gribnames): os.symlink(Gribfile,gribname) # link current file      
    print(('\n  * '+mytag+' converting Grib to WRF IM format (ungrib.exe)'))
    # N.B.: binary mode 'b' is not really necessary on Unix
    # run ungrib.exe
    fungrib = open(self.ungrib_log, 'a') # ungrib.exe output and error log
    subprocess.call([self.UNGRIB], stdout=fungrib, stderr=fungrib)
    fungrib.close() # close log file for ungrib
    for gribname in self.gribnames: os.remove(gribname) # remove link for next step    
    # renaming happens outside, so we don't have to know about metgrid format
    ungribout = self.ungribout.format(*date) # ungrib.exe names output files in a specific format
    return ungribout # return name of output file


## ERA-Interim
class ERAI(Dataset):
  # a class that holds meta data specific to ERA-Interim data
  grbdirs = ['uv','sc','sfc']
  grbstrs = ['ei.oper.an.pl.regn128uv.{:s}','ei.oper.an.pl.regn128sc.{:s}','ei.oper.an.sfc.regn128sc.{:s}']
  dateform = '\d\d\d\d\d\d\d\d\d\d' # YYYYMMDDHH (for matching regex)
  datestr = '{:04d}{:02d}{:02d}{:02d}' # year, month, day, hour (for printing)
  # all other variables have default values

## ERA5 
class ERA5(Dataset):   
  
  # ERA5 data info
  grbdirs = ['pl','sl']
  grbstrs = ['ERA5-{:s}-pl.grib','ERA5-{:s}-sl.grib']
  dateform = '\d\d\d\d\d\d\d\d\d\d\d\d' # YYYYMMDDHHmm (for matching regex).
  datestr = '{:04d}{:02d}{:02d}{:02d}' # Year, month, day, hour.
  yearlyfolders = True # Use subfolders for every year.
  subdform = '\d\d\d\d' # Subdirectories in calendar year format. 
  interval = 3 # ERA5 has 3-hourly data.
  ungribdateout = '{:04d}-{:02d}-{:02d}_{:02d}' # ungrib.exe date output format YYYY-MM-DD_HH.
  # NOTE: Variables, etc not mentioned here have the default values/definitions.
  
  # ERA5 temprorary files
  preimfile = 'FILEOUT' 
    
  # fixIM file and its log
  fixIM = 'fixIM.py'
  fixIM_log = 'fixIM.py.log'
  
  # ================================== __init__ function ==================================
  def __init__(self, folder=None):
    # Checking types, etc
    if not isinstance(self.grbdirs,(list,tuple)): raise TypeError('   Need to define a list of grib folders.')
    if not isinstance(self.grbstrs,(list,tuple)): raise TypeError('   Need to define a list of grib file names.')
    if len(self.grbstrs) != len(self.grbdirs): raise ValueError('   Grid file types and folders need to be of the same number.')
    if len(self.grbstrs) > len(Alphabet): raise ValueError('   Currently only {0:d} file types are supported.'.format(len(Alphabet)))
    if not isinstance(folder,str): raise IOError('   Warning: Need to specify root folder!')    
    # Files and folders    
    self.folder = folder 
    # NOTE: "folder" needs to be set externally for different applications.
    self.GrbDirs = ['{0:s}/{1:s}'.format(folder,grbdir) for grbdir in self.grbdirs]
    # NOTE: "0:" in the above is the first input (folder) and "1:" is the second
    #   input (grbdir). 
    self.UNGRIB = './' + self.ungrib_exe   
    self.FIXIM = ['python',self.fixIM] 
    # Generate required ungrib names
    gribnames = []
    for i in range(len(self.grbstrs)):
      # NOTE: range(X) gives 0, 1, ..., X-1.
      gribname = '{0:s}.AA{1:s}'.format(self.gribname,Alphabet[i])
      gribnames.append(gribname)
    self.gribnames = gribnames    
    # Display that there's yearly subfolder structure
    if self.yearlyfolders: print('\n   Data appears to be stored in yearly subfolders.')
    # Compile regular expressions (needed to extract dates) 
    self.MainDir = os.readlink(self.GrbDirs[0]) # Directory to be searched for dates.
    # NOTE: Python method readlink() returns a string representing the path to which 
    #   the symbolic link points. It may return an absolute or relative pathname.
    self.mainfiles = self.grbstrs[0].format(self.dateform) # Regex definition for master list.
    self.mainrgx = re.compile(self.mainfiles+'$') # Use as master list.  
    self.dateregx = re.compile(self.dateform) # Regex to extract dates from filenames.
    self.subdregx = re.compile(self.subdform+'$') # Subfolder format (at the moment just calendar years). 
    # NOTE: re.compile(pattern) compiles a regular expression pattern into a regular 
    #   expression object, which can be used for matching using its match(), search()
    #   and other methods.   
  
  # ====================== Method to link/copy ungrib_exe, fixIM and vtable ======================   
  def setup(self, src, dst, lsymlink=False):    
    if lsymlink:
      cwd = os.getcwd()
      os.chdir(dst)
      os.symlink(src+self.ungrib_exe, self.ungrib_exe)
      os.symlink(src+self.fixIM, self.fixIM)
      os.symlink(Meta+self.vtable,self.vtable) 
      os.chdir(cwd)
    else:
      shutil.copy(src+self.ungrib_exe, dst)
      shutil.copy(src+self.fixIM, dst)
      shutil.copy(Meta+self.vtable, dst)   
  
  # ======================= Method to remove ungrib_exe, fixIM and vtable ========================  
  def cleanup(self, tgt):
    cwd = os.getcwd()
    os.chdir(tgt)
    os.remove(self.ungrib_exe)
    os.remove(self.fixIM)
    os.remove(self.vtable)
    os.chdir(cwd)

  # ======= Method to determine whether a subfolder contains valid data and can be processed =======
  # ======= recursively. Checks that the subfolder name is a valid calendar year.            =======
  def checkSubDir(self, subdir, start, end):     
    match = self.subdregx.match(subdir)
    if match:      
      lmatch = ( start[0] <= int(subdir) <= end[0] )
    else: lmatch = False 
    return lmatch 

  # ================= Method that generates the WRF IM file for metgrid.exe ===============
  def ungrib(self, date, mytag):  
    # Create formatted date string
    datestr = self.datestr.format(*date) # (years, months, days, hours).
    # Initilize status output message (message printed later)
    msg = datestr+", with files:"    
    # Create links to relevant source data
    Grbfiles = [] # List of relevant source files.    
    for GrbDir,grbstr in zip(self.GrbDirs,self.grbstrs):
      # NOTE: The zip() function returns a zip object, which is an iterator of tuples 
      #   where the first item in each passed iterator is paired together, and then 
      #   the second item in each passed iterator are paired together, etc.
      grbfile = grbstr.format(datestr+'00') # Insert current date.
      Grbfile = '{0:s}/{1:04d}/{2:s}'.format(GrbDir,date[0],grbfile) # Absolute path.
      if not os.path.exists(Grbfile): 
        raise IOError("Input file '{0:s}' not found!".format(Grbfile))     
      else:
        msg += '\n     '+grbfile # Add to output message.
        Grbfiles.append(Grbfile) # Append to file list.
    # Prompt on screen
    print(('\n   '+mytag+' Processing time-step: '+msg))
    # Link    
    for Gribfile,gribname in zip(Grbfiles,self.gribnames): os.symlink(Gribfile,gribname)
    # NOTE: The format for os.symlink is os.symlink(src, dst).       
    # Prompt on screen
    print('\n   * '+mytag+' Converting grib to WRF IM format (ungrib.exe).')    
    # Run ungrib.exe
    fungrib = open(self.ungrib_log, 'a') # ungrib.exe output and error log.
    # NOTE: "a" above means append - opens a file for appending, creates 
    #   the file if it does not exist. 
    subprocess.call([self.UNGRIB], stdout=fungrib, stderr=fungrib)
    # NOTE: The subprocess module allows you to spawn new processes, connect to their 
    #   input/output/error pipes, and obtain their return codes. With subprocess.call() 
    #   you pass an array of commands and parameters. This expects input command and
    #   its parameters inside [].
    fungrib.close() # Close log file for ungrib.    
    # Prompt on screen
    print('\n   * '+mytag+' Fixing WRF IM file (fixIM.py).')    
    # Run fixIM.py
    ffixim = open(self.fixIM_log, 'a') # fixIM.py output and error log.
    call_arg = self.FIXIM + [self.ungribdateout.format(*date)] + Grbfiles + [os.getcwd()+'/'+self.ungribout.format(*date)] 
    subprocess.call(call_arg, stdout=ffixim, stderr=ffixim)
    ffixim.close() # Close log file for fixIM.    
    # Remove links (to prepare for next step)
    for gribname in self.gribnames: os.remove(gribname)    
    # NOTE: os.remove() method is used to remove or delete a file path. This method can 
    #   not remove or delete a directory. If the specified path is a directory then 
    #   OSError will be raised by the method. os.rmdir() can be used to remove directories.    
    return self.preimfile
    # NOTE: Renaming happens outside, so we don't have to know about metgrid format. 

## NARR
class NARR(Dataset):
  # a class that holds meta data specific to ERA-Interim data
  grbdirs = ['plev','flx','sfc']
  grbstrs = ['merged_AWIP32.{:s}.3D','merged_AWIP32.{:s}.RS.flx','merged_AWIP32.{:s}.RS.sfc']
  dateform = '\d\d\d\d\d\d\d\d\d\d' # YYYYMMDDHH (for matching regex)
  datestr = '{:04d}{:02d}{:02d}{:02d}' # year, month, day, hour (for printing)
  interval = 3 # NARR has 3-hourly data
  # all other variables have default values

## CFSR
class CFSR(Dataset):
  # a class that holds meta data and implements operations specific to CFSR data
  # CFSR is special in that surface and pressure level files are handled separately
  # note that this class does not hold any actual data
  # N.B.: ungrib.exe must be Grib2 capable!
  # CFSR data source
  gribname = 'GRIBFILE.AAA' # this is CFSR specific - only one file type is handled at a time  
  tmpfile = 'TMP{:02d}' # temporary files created during ungribbing (including an iterator)
  preimfile = 'FILEOUT'
  datestr = '{:04d}{:02d}{:02d}{:02d}' # year, month, day, hour (for printing)
  # pressure levels (3D)
  plevdir = 'plev'
  plevvtable = 'Vtable.CFSR_plev'
  plevstr = '00.pgbh06.gdas.grb2' # including filename extension
  # surface data
  srfcdir = 'srfc'
  srfcvtable = 'Vtable.CFSR_srfc'
  srfcstr = '00.flxf06.gdas.grb2' # including filename extension

  def __init__(self, folder=None):

    if not isinstance(folder,str): raise IOError('Warning: need to specify root folder!')
    ## CESM specific files and folders (only necessary for file operations)
    self.folder = folder # needs to be set externally for different applications
    self.PlevDir = os.readlink(folder + self.plevdir)
    self.SrfcDir = os.readlink(folder + self.srfcdir)
    self.UNGRIB = './' + self.ungrib_exe
    # use pressure level files as master list
    self.MainDir = self.PlevDir # directory to be searched for dates    
    ## compile regular expressions (needed to extract dates)
    self.mainfiles = self.dateform+self.plevstr # regex definition for master list
    self.mainrgx = re.compile(self.mainfiles+'$') # use as master list
    self.dateregx = re.compile(self.dateform) # regex to extract dates from filenames

  def setup(self, src, dst, lsymlink=False):
    # method to copy dataset specific files and folders working directory
    # executables
    if lsymlink:
      cwd = os.getcwd()
      os.chdir(dst)
      # use current directory
      os.symlink(src+self.ungrib_exe, self.ungrib_exe)
      os.chdir(cwd)
    else:
      shutil.copy(src+self.ungrib_exe, dst)
    # N.B.: the difference to the default method is that CFSR has two Vtables, and not just one
  
  def cleanup(self, tgt):
    # method to remove dataset specific files and links
    cwd = os.getcwd()
    os.chdir(tgt)
    # use current directory
    os.remove(self.ungrib_exe)
    os.chdir(cwd)

  def ungrib(self, date, mytag):
    # method that generates the WRF IM file for metgrid.exe
    # create formatted date string
    datestr = self.datestr.format(*date) # (years, months, days, hours)
    # create links to relevant source data (requires full path for linked files)
    plevfile = datestr+self.plevstr; Plevfile = self.PlevDir+plevfile
    if not os.path.exists(Plevfile): 
      raise IOError("Pressure level input file '{:s}' not found!".format(Plevfile))     
    srfcfile = datestr+self.srfcstr; Srfcfile = self.SrfcDir+srfcfile
    if not os.path.exists(Srfcfile): 
      raise IOError("Surface input file '{:s}' not found!".format(Srfcfile))     
    # print feedback
    print(('\n '+mytag+' Processing time-step:  '+datestr+'\n    '+plevfile+'\n    '+srfcfile))
    gribfiles = (Plevfile, Srfcfile)
    vtables = (self.plevvtable, self.srfcvtable)
#     else:
#       print('\n '+mytag+' Processing time-step:  '+datestr+'\n    '+plevfile)
#       print('\n '+mytag+'   ***   WARNING: no surface data - this may not work!   ***')
#       gribfiles = (Plevfile,)
#       vtables = (self.plevvtable,)      
    ## loop: process grib files and concatenate resulting IM files     
    print(('\n  * '+mytag+' converting Grib2 to WRF IM format (ungrib.exe)'))
    ungribout = self.ungribout.format(*date) # ungrib.exe names output files in a specific format
    preimfile = open(self.preimfile,'wb') # open final (combined) WRF IM file 
    # N.B.: binary mode 'b' is not really necessary on Unix
    fungrib = open(self.ungrib_log, 'a') # ungrib.exe output and error log
    for i in range(len(gribfiles)):
      os.symlink(gribfiles[i],self.gribname) # link current file
      os.symlink(Meta+vtables[i],self.vtable) # link VTable
      # run ungrib.exe
      subprocess.call([self.UNGRIB], stdout=fungrib, stderr=fungrib)
      os.remove(self.gribname) # remove link for next step
      os.remove(self.vtable) # remove link for next step
      # append output to single WRF IM files (preimfile)
      shutil.copyfileobj(open(ungribout,'rb'),preimfile)
      os.remove(ungribout) # cleanup for next file      
    # finish concatenation of ungrib.exe output
    preimfile.close()
    fungrib.close() # close log file for ungrib    
    # renaming happens outside, so we don't have to know about metgrid format
    return self.preimfile
  
## CESM
class CESM(Dataset):
  # a class that holds meta data and implements operations specific to CESM data
  # note that this class does not hold any actual data
  # unccsm executables
  unncl_ncl = 'unccsm.ncl'
  unncl_log = 'unccsm.ncl.log'
  unccsm_exe = 'unccsm.exe'
  unccsm_log = 'unccsm.exe.log'
  # unccsm temporary files
  nclfile = 'intermed.nc'
  preimfile = 'FILEOUT' 
  # CESM data source
  prefix = '' # 'cesm19752000v2', 'cesmpdwrf1x1'
  ncext = ncext
  dateform = '\d\d\d\d-\d\d-\d\d-\d\d\d\d\d'
  datestr = '{:04d}-{:02d}-{:02d}-{:05d}' # year, month, day, seconds
  yearlyfolders = False # use subfolders for every year
  subdform = '\d\d\d\d' # subdirectories in calendar year format 
  # atmosphere
  atmdir = 'atm/'
  atmpfx = '.cam2.h1.'
  atmlnk = 'atmfile.nc'
  # land
  lnddir = 'lnd/'
  lndpfx = '.clm2.h1.'
  lndlnk = 'lndfile.nc'
  # ice
  icedir = 'ice/'
  icepfx = '.cice.h1_inst.'
  icelnk = 'icefile.nc'

  def __init__(self, folder=None, prefix=None):
    
    if not isinstance(folder,str): raise IOError('Warning: need to specify root folder!')    
    ## CESM specific files and folders (only necessary for file operations)
    self.folder = folder # needs to be set externally for different applications
    self.AtmDir = os.readlink(folder + self.atmdir[:-1])
    self.LndDir = os.readlink(folder + self.lnddir[:-1])
    self.IceDir = os.readlink(folder + self.icedir[:-1])
    self.NCL_ETA2P = NCL + ' ' + self.unncl_ncl
    self.UNCCSM = './' + self.unccsm_exe
    # set environment variable for NCL (on tmp folder)   
    os.putenv('NCARG_ROOT', NCARG) 
    os.putenv('NCL_POP_REMAP', meta) # NCL is finicky about space characters in the path statement, so relative path is saver
    os.putenv('CODE_ROOT', Model) # also for NCL (where personal function libs are)
      
    # figure out source file prefix (only needs to be determined once)
    if not prefix: 
      # get file prefix for data files
      # use only atmosphere files
      prergx = re.compile(self.atmpfx+self.dateform+self.ncext+'$')
      # recursive function to search for first valid filename in subfolders
      def searchValidName(SearchFolder):
        prfx = None
        for filename in os.listdir(SearchFolder):
          TmpDir = SearchFolder+'/'+filename
          if os.path.isdir(TmpDir):
            prfx = searchValidName(TmpDir) # recursion
            if prfx: self.yearlyfolders = True
          else:
            match = prergx.search(filename) 
            if match: prfx = filename[0:match.start()] # use everything before the pattern as prefix
          if prfx: break
        return prfx
      # find valid file name in atmosphere directory
      prefix = searchValidName(self.AtmDir)
      # print prefix
      print(('\n No data prefix defined; inferring prefix from valid data files in directory '+self.AtmDir))
      print(('  prefix = '+prefix))
    if prefix: self.atmpfx = prefix+self.atmpfx
    if prefix: self.lndpfx = prefix+self.lndpfx
    if prefix: self.icepfx = prefix+self.icepfx
    self.prefix = prefix
    
    # identify subfolder structure
    if self.yearlyfolders: print('\n Data appears to be stored in yearly subfolders.')

    ## compile regular expressions (needed to extract dates)
    # use atmosphere files as master list 
    self.MainDir = self.AtmDir
    self.mainfiles = self.atmpfx+self.dateform+self.ncext
    self.mainrgx = re.compile(self.mainfiles+'$') # use atmosphere files as master list
    # regex to extract dates from filenames
    self.dateregx = re.compile(self.dateform)
    # subfolder format (at the moment just calendar years)
    self.subdregx = re.compile(self.subdform+'$')
      
  def checkSubDir(self, subdir, start, end):
    # method to determine whether a subfolder contains valid data and can be processed recursively
    # check that the subfolder name is a valid calendar year 
    match = self.subdregx.match(subdir)
    if match:      
      # test that it is within the right time period
      lmatch = ( start[0] <= int(subdir) <= end[0] )
    else: lmatch = False
    # return results 
    return lmatch 
    
  def extractDate(self, filename): # , zero=2000
    # method to generate date tuple from date string in filename
    # match valid filenames
    match = self.mainrgx.match(filename) # return match object
    if match is None:
      return None # if the filename doesn't match the regex
    else:
      # extract date string
      datestr = self.dateregx.search(filename).group()
      # split date string into tuple 
      year, month, day, second = datestr.split('-')
#      if year[0] == '0': year = int(year)+zero # start at year 2000 (=0000)
      year = int(year)
      month = int(month); day = int(day)
      hour = int(second)//3600 
      return (year, month, day, hour)
    
  def constructDateList(self, start, end):
    curd = dt.datetime(*start); endd = dt.datetime(*end) # datetime objects
    delta = dt.timedelta(hours=self.interval) # usually an integer in hours...
    dates = [] # create date list
    while curd <= endd:
        if not (curd.month == 2 and curd.day == 29):
            dates.append((curd.year, curd.month, curd.day, curd.hour))
        curd += delta # increment date by interval
    # return properly formated list
    return dates
  
  def setup(self, src, dst, lsymlink=False):          
    # method to copy dataset specific files and folders working directory
    # executables   
    if lsymlink:
      cwd = os.getcwd()
      os.chdir(dst)
      # use current directory
      os.symlink(src+self.unccsm_exe, self.unccsm_exe)
      os.symlink(src+self.unncl_ncl, self.unncl_ncl)
      os.chdir(cwd)
    else:
      shutil.copy(src+self.unccsm_exe, dst)
      shutil.copy(src+self.unncl_ncl, dst)

  def cleanup(self, tgt):
    # method to remove dataset specific files and links
    cwd = os.getcwd()
    os.chdir(tgt)
    # use current directory    
    os.remove(self.unccsm_exe)
    os.remove(self.unncl_ncl)
    os.chdir(cwd)

  def ungrib(self, date, mytag):
    # method that generates the WRF IM file for metgrid.exe
    # create formatted date string
    datestr = self.datestr.format(date[0],date[1],date[2],date[3]*3600) # not hours, but seconds...
    # create links to relevant source data (requires full path for linked files)
    atmfile = self.atmpfx+datestr+self.ncext
    if self.yearlyfolders: atmfile = '{:04d}/{:s}'.format(date[0],atmfile) 
    if not os.path.exists(self.AtmDir+atmfile): 
      raise IOError("Atmosphere input file '{:s}' not found!".format(self.AtmDir+atmfile))
    os.symlink(self.AtmDir+atmfile,self.atmlnk)
    lndfile = self.lndpfx+datestr+self.ncext
    if self.yearlyfolders: lndfile = '{:04d}/{:s}'.format(date[0],lndfile)
    if not os.path.exists(self.LndDir+lndfile): 
      raise IOError("Land surface input file '{:s}' not found!".format(self.LndDir+lndfile))
    os.symlink(self.LndDir+lndfile,self.lndlnk)
    icefile = self.icepfx+datestr+self.ncext
    if self.yearlyfolders: icefile = '{:04d}/{:s}'.format(date[0],icefile)
    if not os.path.exists(self.IceDir+icefile): 
      raise IOError("Seaice input file '{:s}' not found!".format(self.IceDir+icefile))
    os.symlink(self.IceDir+icefile,self.icelnk)
    # print feedback
    print(('\n '+mytag+' Processing time-step:  '+datestr+'\n    '+atmfile+'\n    '+lndfile+'\n    '+icefile))
    #else: print('\n '+mytag+' Processing time-step:  '+datestr+'\n    '+atmfile+'\n    '+lndfile)
    
    ##  convert data to intermediate files (run unccsm tool chain)
    # run NCL script (suppressing output)
    print(('\n  * '+mytag+' interpolating to pressure levels (eta2p.ncl)'))
    fncl = open(self.unncl_log, 'a') # NCL output and error log
    # on SciNet we have to pass this command through the shell, so that the NCL module is loaded.
    subprocess.call(self.NCL_ETA2P, shell=True, stdout=fncl, stderr=fncl)
    ## otherwise we don't need the shell and it's a security risk
    #subprocess.call([NCL,self.unncl_ncl], stdout=fncl, stderr=fncl)
    fncl.close()
    # run unccsm.exe
    print(('\n  * '+mytag+' writing to WRF IM format (unccsm.exe)'))
    funccsm = open(self.unccsm_log, 'a') # unccsm.exe output and error log
    subprocess.call([self.UNCCSM], stdout=funccsm, stderr=funccsm)   
    funccsm.close()
    # cleanup
    os.remove(self.atmlnk); os.remove(self.lndlnk); os.remove(self.icelnk)
    os.remove(self.nclfile)    # temporary file generated by NCL script 
    # renaming happens outside, so we don't have to know about metgrid format
    return self.preimfile

#====================================================================================
## CMIP5
class CMIP5(Dataset):
  # a class that holds meta data and implements operations specific to CMIP5 data
  # individual CMIP5 experiment may require furthre customization
  # unCMIP5 executables and validate file
  unncl_ncl = 'unCMIP5.ncl'
  unncl_log = 'unCMIP5.ncl.log'
  unccsm_exe = 'unccsm.exe'
  unccsm_log = 'unccsm.exe.log'
  validate_file = 'CMIP5data.validate.nc'
  grid_file_orog = 'orog_file.nc'
  grid_file_sftlf = 'sftlf_file.nc'
  weight_file = 'ocn2atmweight_file.nc'
  # unCMIP5 temporary files
  cdbfile_6hourly = 'merged_6hourly.nc'    #temporary file generated by call_cdb_query
  cdbfile_daily = 'merged_daily.nc'
  cdbfile_monthly = 'merged_monthly.nc'
  nclfile = 'intermed.nc'
  preimfile = 'FILEOUT' 
  # CMIP5 data source
  # Using cdb_query means only the validator file along with call_cdb_query function is needed.
  # However, a flaw in cdb_query requires the initial setep file for each year to be treated separately
  prefix = '' # 'cesm19752000v2', 'cesmpdwrf1x1'
  ncext = ncext
  # initial file setup
  stepIdir = 'init/'
  stepIpfx = '/initial'
  stepIlnk = 'initialstepfile.nc'

  def __init__(self, folder=None, prefix=None):
    
    if not isinstance(folder,str): raise IOError('Warning: need to specify root folder!')    
    ## CMIP5 specific files and folders (only necessary for file operations)
    self.folder = folder # needs to be set externally for different applications
    self.StepIDir = os.readlink(folder + self.stepIdir[:-1])
    self.NCL_ETA2P = NCL + ' ' + self.unncl_ncl
    self.UNCCSM = './' + self.unccsm_exe
    # set environment variable for NCL (on tmp folder)   
    os.putenv('NCARG_ROOT', NCARG) 
    os.putenv('NCL_POP_REMAP', meta) # NCL is finicky about space characters in the path statement, so relative path is saver
    os.putenv('CODE_ROOT', Model) # also for NCL (where personal function libs are)
    self.MainDir = None    #no directory needed!
    self.validate_file = 'CMIP5data.validate.nc'
    self.grid_file_orog = 'orog_file.nc'
    self.grid_file_sftlf = 'sftlf_file.nc'
    self.weight_file = 'ocn2atmweight_file.nc'
      
    # prefix not needed by cdb_query
      
#  def checkSubDir(self, subdir, start, end):
    # no subfolder is used by cdb_query
    
#  def extractDate(self, filename): # , zero=2000
    # method to generate date tuple from date string in filename
    # no valid filenames will be provided by the validate file, and the datelist will always be constructed.
    
  def constructDateList(self, start, end):
    # CMIP5 data should work the same as CESM data with no leap years
    curd = dt.datetime(*start); endd = dt.datetime(*end) # datetime objects
    delta = dt.timedelta(hours=self.interval) # usually an integer in hours...
    dates = [] # create date list
    while curd <= endd:
        if not (curd.month == 2 and curd.day == 29):
            dates.append((curd.year, curd.month, curd.day, curd.hour))
        curd += delta # increment date by interval
    # return properly formated list
    return dates
  
  def setup(self, src, dst, lsymlink=False):          
    # method to copy dataset specific files and folders working directory
    # executables   
    if lsymlink:
      cwd = os.getcwd()
      os.chdir(dst)
      # use current directory
      os.symlink(src+self.unccsm_exe, self.unccsm_exe)
      os.symlink(src+self.unncl_ncl, self.unncl_ncl)
      os.symlink(src+self.validate_file, self.validate_file)
      os.symlink(src+self.grid_file_orog, self.grid_file_orog)
      os.symlink(src+self.grid_file_sftlf, self.grid_file_sftlf)
      os.symlink(src+self.weight_file, self.weight_file)
      os.chdir(cwd)
    else:
      shutil.copy(src+self.unccsm_exe, dst)
      shutil.copy(src+self.unncl_ncl, dst)
      # the validate file is too large to be copied into the directory, only links are proper!

  def cleanup(self, tgt):
    # method to remove dataset specific files and links
    cwd = os.getcwd()
    os.chdir(tgt)
    # use current directory    
    os.remove(self.unccsm_exe)
    os.remove(self.unncl_ncl)
    #os.remove(self.validate_file)
    #os.remove(self.grid_file_orog)
    #os.remove(self.grid_file_sftlf)
    os.chdir(cwd)

  def ungrib(self, date, mytag):
    # method that generates the WRF IM file for metgrid.exe
    # setup link to the initial step files that cdb_query cannot generate
    stepIfile = self.stepIpfx+str(date[0])+self.ncext
    #if self.yearlyfolders: atmfile = '{:04d}/{:s}'.format(date[0],atmfile) 
    if not os.path.exists(self.StepIDir+stepIfile): 
      raise IOError("Initial step input file '{:s}' not found!".format(self.StepIDir+stepIfile))
    os.symlink(self.StepIDir+stepIfile,self.stepIlnk)
    os.system('ls -l')
    # the date list would work directly as input to call_cdb_query
    # call_cdb_query would take care of data slicing&merging from source data directory, and feedback.
    apply_cdb_query_singleWPSstep(self.validate_file,date)
    #else: print('\n '+mytag+' Processing time-step:  '+datestr+'\n    '+atmfile+'\n    '+lndfile)
    ##  convert data to intermediate files (run unccsm tool chain)
    # run NCL script (suppressing output)
    print(('\n  * '+mytag+' interpolating to pressure levels (eta2p.ncl)'))
    fncl = open(self.unncl_log, 'a') # NCL output and error log
    # on SciNet we have to pass this command through the shell, so that the NCL module is loaded.
    subprocess.call(self.NCL_ETA2P, shell=True, stdout=fncl, stderr=fncl)
    ## otherwise we don't need the shell and it's a security risk
    #subprocess.call([NCL,self.unncl_ncl], stdout=fncl, stderr=fncl)
    fncl.close()
    # run unccsm.exe
    print(('\n  * '+mytag+' writing to WRF IM format (unccsm.exe)'))
    funccsm = open(self.unccsm_log, 'a') # unccsm.exe output and error log
    subprocess.call([self.UNCCSM], stdout=funccsm, stderr=funccsm)   
    funccsm.close()
    # cleanup
    os.remove(self.stepIlnk)
    os.remove(self.cdbfile_6hourly); os.remove(self.cdbfile_daily); os.remove(self.cdbfile_monthly);     # temporary file generated by call_cdb_query
    os.remove(self.nclfile)    # temporary file generated by NCL script 
    # renaming happens outside, so we don't have to know about metgrid format
    return self.preimfile
    
## CMIP6 
class CMIP6(Dataset):   
  
  # CMIP6 data info
  datestr = '{:04d}{:02d}{:02d}{:02d}' # Year, month, day, hour.
  
  # ERA5 temprorary files
  preimfile = 'CMIP6' 
    
  # uncmip6 and its log file
  uncmip6 = 'unCMIP6.py'
  uncmip6_log = 'unCMIP6.py.log'
  
  # ungrib output date format (uncmip6 uses the same format)
  ungribdateout = '{:04d}-{:02d}-{:02d}_{:02d}' # uncmip6 output format YYYY-MM-DD_HH.
  
  # Data folder
  cmip6_dir = 'cmip6_data/'
  cmip6_dir_link = 'cmip6_data'
  
  # Which models are leap and which are noleap
  models_noleap = {
    'MPI-ESM1-2-HR':False,
    'CESM2':True,
    'MRI-ESM2-0':False
    }
  
  # ================================== __init__ function ==================================
  def __init__(self, folder=None):
    # Checking folder
    if not isinstance(folder,str): raise IOError('   Warning: Need to specify root folder!')    
    # Files and folders    
    self.folder = folder 
    self.CMIP6_DIR = os.readlink(folder+self.cmip6_dir[:-1])
    self.modelname = self.CMIP6_DIR.split(".")[3]
    # NOTE: Here we assume that there are no other dots in the dir name other than those
    #   associated with the seperators for the CMIP6 name.
    # Find if the model is leap or not
    self.noleap = self.models_noleap[self.modelname]
    self.MainDir = None  # No directory needed.
    # NOTE: "folder" needs to be set externally for different applications. 
    self.UNCMIP6 = ['python3',self.uncmip6]   

  # ============= Construct a list of dates where data should be available ==============
  def constructDateList(self, start, end):
    # For some CMIP6 models calendar is leap and some have no leap calendar
    curd = dt.datetime(*start); endd = dt.datetime(*end) # Datetime objects.
    # NOTE: In a function call, * unpacks a list or tuple into position arguments,
    #   whereas ** unpacks a dictionary into keyword arguments.
    delta = dt.timedelta(hours=self.interval) # Usually an integer in hours.
    dates = [] # Create date list.
    while curd <= endd:
        if not(curd.month == 2 and curd.day == 29 and self.noleap):
            dates.append((curd.year, curd.month, curd.day, curd.hour)) # Format: year, month, day, hour.
        curd += delta # Increment date by interval.
    return dates # Return properly formated list.    
  
  # ====================== Method to link/copy uncmip6 and vtable ======================   
  def setup(self, src, dst, lsymlink=False):    
    if lsymlink:
      cwd = os.getcwd()
      os.chdir(dst)
      os.symlink(src+self.uncmip6,self.uncmip6)
      os.symlink(Meta+self.vtable,self.vtable) 
      os.chdir(cwd)
    else:
      shutil.copy(src+self.uncmip6,dst)
      shutil.copy(Meta+self.vtable,dst)   
  
  # ======================= Method to remove uncmip6 and vtable ========================  
  def cleanup(self, tgt):
    cwd = os.getcwd()
    os.chdir(tgt)
    os.remove(self.uncmip6)
    os.remove(self.vtable)
    os.chdir(cwd)

  # ================= Method that generates the WRF IM file for metgrid.exe ===============
  def ungrib(self, date, mytag):  
    # Link the data folder
    if not os.path.exists(self.CMIP6_DIR): 
      raise IOError("Data directory link not found!")
    if not(os.path.exists(self.cmip6_dir_link)):
        os.symlink(self.CMIP6_DIR,self.cmip6_dir_link)
    # Create formatted date string
    datestr = self.datestr.format(*date) # (years, months, days, hours).    
    # Prompt on screen
    print(('\n   '+mytag+' Processing time-step: '+datestr))       
    # Prompt on screen
    print('\n   * '+mytag+' Converting input files to WRF IM format (unCMIP6.py).')    
    # Run unCMIP6.py
    funcmip6 = open(self.uncmip6_log, 'a') # uncmip6 output and error log.
    call_arg = self.UNCMIP6 + [datestr,self.cmip6_dir_link]
    subprocess.call(call_arg, stdout=funcmip6, stderr=funcmip6)
    funcmip6.close() # Close log file for uncmip6.        
    return self.preimfile+':'+self.ungribdateout.format(*date)
    # NOTE: Renaming happens outside, so we don't have to know about metgrid format.
    
    
## import local settings from file
#sys.path.append(os.getcwd()+'/meta')
#from namelist import *
#print('\n Loading namelist parameters from '+meta+'/namelist.py:')
#import imp # to import namelist variables
#nmlstpy = imp.load_source('namelist_py',meta+'/namelist.py') # avoid conflict with module 'namelist'
#localvars = locals()
## loop over variables defined in module/namelist  
#for var in dir(nmlstpy):
#  if ( var[0:2] != '__' ) and ( var[-2:] != '__' ):
#    # overwrite local variables
#    localvars[var] = nmlstpy.__dict__[var]
#    print('   '+var+' = '+str(localvars[var]))
#print('')


## subroutines

## function to divide a list fairly evenly 
def divideList(genericlist, n):
  nlist = len(genericlist) # total number of items
  items = nlist // n # items per sub-list
  rem = nlist - items*n
  # distribute list items
  listoflists = []; ihi = 0 # initialize
  for i in range(n):
    ilo = ihi; ihi += items # next interval
    if i < rem: ihi += 1 # these intervals get one more
    listoflists.append(genericlist[ilo:ihi]) # append interval to list of lists
  # return list of sublists
  return listoflists

## parallel pre-processing function
# N.B.: this function has some shared variables for folder names and regx'
# function to process filenames and check dates
def processFiles(qfilelist, qListDir, queue):
#  # some old code with interesting regex handling
#  files = [atmrgx.match(file) for file in filelist] # parse (partial) filelist for atmospheric model (CAM) output  
#  atmfiles = [match.group() for match in files if not match is None] # list of time steps from atmospheric output
#  files = [dateregx.search(atmfile) for atmfile in atmfiles]
#  dates = [match.group() for match in files if not match is None]
  # function to check filenames and subfolders recursively
  def checkFileList(filelist, ListDir, okdates, depth):
    depth += 1 # counter for recursion depth
    # N.B.: the recursion depth limit was introduced to prevent infinite recursions when circular links occur
    # loop over dates
    for filename in filelist:
      TmpDir = ListDir + '/' + filename
      if os.path.isdir(TmpDir):
        if dataset.checkSubDir(filename, starts[0], ends[0]):          
          # make list of contents and process recursively
          if depth > 1: print(' (skipping subfolders beyond recursion depth/level 1)')
          else: okdates = checkFileList(os.listdir(TmpDir), TmpDir, okdates, depth)
      else:
        # figure out time and date
        date = dataset.extractDate(filename)
        # collect valid dates
        if date: # i.e. not 'None'
          # check date for validity (only need to check first/master domain)      
          lok = nlt.checkDate(date, starts[0], ends[0])
          # collect dates within range
          if lok: okdates.append(date)
    return okdates
  # start checking file list (start with empty results list)
  qokdates = checkFileList(qfilelist, qListDir, [], 0)   
  # return list of valid datestrs
  queue.put(qokdates)
  

## primary parallel processing function: workload for each process
# N.B.: this function has a lot of shared variable for folder and file names etc.
# this is the actual processing pipeline
def processTimesteps(myid, dates):
  
  # create process sub-folder
  mydir = pdir.format(myid)
  MyDir = Tmp + mydir
  mytag = '['+pname.format(myid)+']'
  if os.path.exists(mydir): 
    shutil.rmtree(mydir)
  os.mkdir(mydir)
  # copy namelist
  shutil.copy(nmlstwps, mydir)
  # change working directory to process sub-folder
  os.chdir(mydir)
  # link dataset specific files
  dataset.setup(src=Tmp, dst=MyDir, lsymlink=True)
  # link other source files
  os.symlink(Meta, meta[:-1]) # link to folder
  # link geogrid (data) and metgrid
  os.symlink(Tmp+metgrid_exe, metgrid_exe)
  for i in doms: # loop over all geogrid domains
    geoname = geopfx.format(i)+ncext
    os.symlink(Tmp+geoname, geoname)
  
  ## loop over (atmospheric) time steps
  if dates: print(('\n '+mytag+' Looping over Time-steps:'))
  else: print(('\n '+mytag+' Nothing to do!'))
  # loop over date-tuples
  for date in dates:
    
    # figure out sub-domains
    ldoms = [True,]*maxdom # first domain is always computed
    for i in range(1,maxdom): # check sub-domains
      ldoms[i] = nlt.checkDate(date, starts[i], ends[i])
    # update date string in namelist.wps
    #print(imform,date)
    imdate = imform.format(*date)    
    imfile = impfx+imdate
    nmldate = nmlform.format(*date) # also used by metgrid
    nlt.writeNamelist(nmlstwps, ldoms, nmldate, imd, isd, ied)
    
    # N.B.: in case the stack size limit causes segmentation faults, here are some workarounds
    # subprocess.call(r'ulimit -s unlimited; ./unccsm.exe', shell=True)
    # import resource
    # subprocess.call(['./unccsm.exe'], preexec_fn=resource.setrlimit(resource.RLIMIT_STACK,(-1,-1)))
    # print resource.getrlimit(resource.RLIMIT_STACK)
      
    ## prepare WPS processing 
    # run ungrib.exe or equivalent operation
    preimfile = dataset.ungrib(date, mytag) # need 'mytag' for status messages
    # rename intermediate file according to WPS convention (by date), if necessary
    if preimfile: os.rename(preimfile, imfile) # not the same as 'move'
    
    ## run WPS' metgrid.exe on intermediate file
    # run metgrid_exe.exe
    print(('\n  * '+mytag+' interpolating to WRF grid (metgrid.exe)'))
    fmetgrid = open(metgrid_log, 'a') # metgrid.exe standard out and error log    
    subprocess.call(['mpirun', '-n', '1', METGRID], stdout=fmetgrid, stderr=fmetgrid) # metgrid.exe writes a fairly detailed log file
    # N.B.: for some reason, in this contect it is necessary to execute metgrid.exe with the MPI call
    fmetgrid.close()
    
    ## finish time-step
    os.remove(MyDir+imfile) # remove intermediate file after metgrid.exe completes
    # copy/move data back to disk (one per domain) and/or keep in memory
    tmpstr = '\n '+mytag+' Writing output to disk: ' # gather output for later display
    for i in range(maxdom):
      metfile = metpfx.format(i+1)+nmldate+ncext
      if ldoms[i]:
        tmpstr += '\n                           '+metfile
        if ldisk: 
          shutil.copy(metfile,Disk+metfile)
        if ldata:
          shutil.move(metfile,Data+metfile)      
        else:
          os.remove(metfile)
      else:
        if os.path.exists(metfile): 
          os.remove(metfile) # metgrid.exe may create more files than needed
    # finish time-step
    tmpstr += '\n\n   ============================== finished '+imdate+' ==============================   \n'
    print(tmpstr)    
    
      
  ## clean up after all time-steps
  # link other source files
  os.remove(meta[:-1]) # link to folder
  dataset.cleanup(tgt=MyDir)
  os.remove(metgrid_exe)
  for i in doms: # loop over all geogrid domains
    os.remove(geopfx.format(i)+ncext)
    
    
if __name__ == '__main__':
      
        
    ##  prepare environment
    # figure out root folder
    Root = os.getcwd() + '/' # use current directory
    # direct temporary storage
    if lram:       
      Tmp = Ram + tmp # direct temporary storage to ram disk
      if ldata: Data = Ram + data # temporary data storage (in memory)
#      # provide link to ram disk directory for debugging      
#      if not (os.path.isdir(ramlnk) or os.path.islink(ramlnk[:-1])):
#        os.symlink(Ram, ramlnk)
    else:      
      Tmp = Root + tmp # use local directory
      if ldata: Data = Root + data # temporary data storage (just moves here, no copy)      
    # create temporary storage  (file system or ram disk alike)
    if os.path.isdir(Tmp):       
      shutil.rmtree(Tmp) # clean out entire directory
    os.mkdir(Tmp) # otherwise create folder 
    # create temporary data collection folder
    if ldata:
      if os.path.isdir(Data) or os.path.islink(Data[:-1]):
        # remove directory if already there
        shutil.rmtree(Data)
      os.mkdir(Data) # create data folder in temporary storage
    # create/clear destination folder
    if ldisk:
      if not Disk: 
        Disk = Root + disk
      if not ( os.path.isdir(Disk) or os.path.islink(Disk[:-1]) ):
        # create new destination folder
        os.mkdir(Disk)
      ## remove directory if already there
      #shutil.rmtree(Disk)
      #os.mkdir(Disk)
      
    # directory shortcuts
    Meta = Tmp + meta
    # parse namelist parameters
    imd, maxdom, isd, startdates, ied, enddates = nlt.readNamelist(nmlstwps)
    # translate start/end dates into numerical tuples
    starts = [nlt.splitDateWRF(sd) for sd in startdates]
    ends = [nlt.splitDateWRF(ed) for ed in enddates]
    # figure out domains
    doms = list(range(1,maxdom+1)) # list of domain indices
        
    # copy meta data to temporary folder
    shutil.copytree(meta,Meta)
    shutil.copy(metgrid_exe, Tmp)
    shutil.copy(nmlstwps, Tmp)
    for i in doms: # loop over all geogrid domains
      shutil.copy(geopfx.format(i)+ncext, Tmp)
    # copy additioal data required by CMIP5 dataset
    if dataset == 'CMIP5':
      shutil.copy(validate_file, Tmp)
      shutil.copy(grid_file_orog, Tmp)
      shutil.copy(grid_file_sftlf, Tmp)
      shutil.copy(weight_file, Tmp)
    # N.B.: shutil.copy copies the actual file that is linked to, not just the link
    # change working directory to tmp folder
    os.chdir(Tmp)
    
    # create dataset instance
    if dataset  == 'CESM': 
      dataset = CESM(folder=Root)
      ldiscover = True # temporary, until the CESM part is implemented
    elif dataset == 'CMIP5':
      dataset = CMIP5(folder=Root)
    elif dataset == 'CMIP6':
      dataset = CMIP6(folder=Root)  
    elif dataset  == 'CFSR': 
      dataset = CFSR(folder=Root)
    elif dataset  == 'ERA-I': 
      dataset = ERAI(folder=Root)    
    elif dataset  == 'ERA5': 
      dataset = ERA5(folder=Root)
    elif dataset  == 'NARR': 
      dataset = NARR(folder=Root)    
    else:
      raise ValueError('Unknown dataset type: {}'.format(dataset))
      #dataset = CESM(folder=Root) # for backwards compatibility
    # setup working directory with dataset specific stuff
    dataset.setup(src=Root, dst=Tmp) #
    DataDir = dataset.MainDir # should be absolute path
    
    
    ## multiprocessing
    
    # get date list
    if ldiscover: 
      # search for files and check dates for validity
      listoffilelists = divideList(os.listdir(DataDir), NP)
      # divide file processing among processes
      procs = []; queues = []
      for n in range(NP):
        pid = n + 1 # start from 1, not 0!
        q = multiprocessing.Queue()
        queues.append(q)
        p = multiprocessing.Process(name=pname.format(pid), target=processFiles, args=(listoffilelists[n], DataDir, q))
        procs.append(p)
        p.start() 
      # terminate sub-processes and collect results    
      dates = [] # new date list with valid dates only
      for n in range(NP):
        dates += queues[n].get()
        procs[n].join()
    else:
      # construct date list based on dataset
      dates = dataset.constructDateList(starts[0],ends[0])
      
    # report suspicious behaviour
    if len(dates) == 0: raise IOError("No matching input files found for regex '{:s}' ".format(dataset.mainfiles))
    #print dates
    
    # divide up dates and process time-steps
    listofdates = divideList(dates, NP)
    # create processes
    procs = []
    for n in range(NP):
      pid = n + 1 # start from 1, not 0!
      p = multiprocessing.Process(name=pname.format(pid), target=processTimesteps, args=(pid, listofdates[n]))
      procs.append(p)
      p.start()     
    # terminate sub-processes
    for p in procs:
      p.join()
      
    # clean up files
    os.chdir(Tmp)
    dataset.cleanup(tgt=Tmp)
    os.remove(metgrid_exe)
    # N.B.: remember to remove *.nc files in meta-folder!
