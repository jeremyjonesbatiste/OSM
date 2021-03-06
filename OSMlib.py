import numpy as np
import pyfits
import random
import csv
import time
from scipy.spatial import ConvexHull
from scipy.interpolate import interp1d
from scipy.interpolate import interp2d
import inside
from astropy.io import ascii
import os
from scipy.special import jn

from pyfft.cuda import Plan
import pycuda.driver as cuda
from pycuda.tools import make_default_context
import pycuda.autoinit
import pycuda.gpuarray as gpuarray

#Constants
NG=6.67384e-8 #Newton's Gravity in cm^3/g/s^2
R_sun=6.955e10 #Solar Radius in cm
M_sun=1.988435e33 #Solar Mass in g
L_sun=3.839e33 #Solar Luminosity in erg/s
pc=3.08567758e18 #1 parsec in cm
sigma_SB=5.6704e-5 #Stefan-Boltzmann constant in erg/cm^2/s/K^4
h=6.626e-27 #Planck's constant in cm^2*g/s
c=3e10 #Speed of light, cm/s
k=1.381e-16 #Boltzmann constant erg/K

def osm(p,data):
	"""osm = Oblate Star Model
	This function calculates the total chi^2 (from photometry and visibilities)
	Inputs:
	p
		The list of variables to be altered by the fitting function.
		It includes [R_e,vel,inc,T_p,pa] (The equatorial radius, equatorial rotation velocity,
		inclination, polar temperature, and position angle)
	data
		The list of items that need to be passed on to the function. What data includes is listed below.
		
	Outputs:
	chi2
		The total chi^2 (chi^2_phot+chi^2_vis)
	"""
	base_chi2,m,beta,dist,vis,vis_err,phot_data,wl,u_l,v_l,u_m,v_m,uni_wl,uni_dwl,g_scale,phx_dir,use_Z,use_filts,filt_dict,zpf,cwl,phx_dict,colat_len,phi_len,cal,star,model,model_dir,mode=data
	
	ministarttime=time.time()
	#print p
	R_e=p[0]
	vel=p[1]
	inc=p[2]
	T_p=p[3]
	pa=p[4]
	sin_inc=np.sin(inc)
	cos_inc=np.cos(inc)
	sin_pa=np.sin(pa)
	cos_pa=np.cos(pa)
	#try:
	n_params=5.
	
	#Define the latitude/longitude grid
	colat=unitrange(colat_len-1)*np.pi
	sin_colat=np.zeros(len(colat))
	cos_colat=np.zeros(len(colat))
	phi=unitrange(phi_len-1)*2.*np.pi
	sin_phi=np.zeros(len(phi))
	cos_phi=np.zeros(len(phi))
	for i in range(len(colat)):
		sin_colat[i]=np.sin(colat[i])
		cos_colat[i]=np.cos(colat[i])
	for i in range(len(phi)):
		sin_phi[i]=np.sin(phi[i])
		cos_phi[i]=np.cos(phi[i])
	
	#Calculated values
	R_p=1./(1./R_e+(vel*1e5)**2./(2.*(NG*M_sun/R_sun)*m))	#Polar Radius
	#print 'R_p: ',R_p
	w_0=(vel*1e5)**2.*R_p/(2.*(NG*M_sun/R_sun)*m)
	lomg=np.sqrt(27./4.*w_0*(1.-w_0)**2.)			#angular rotational velocity relative to the critical
	if 'z' in mode:
		beta=0.25
	if 'r' in mode:
		beta=calc_beta(lomg)
	OMG_crit=np.sqrt(8./27.*NG*m*M_sun/(R_p*R_sun)**3.)	#Critical angular rotational velocity
	OMG=lomg*OMG_crit				#angular rotational velocity
	g_p=NG*(m*M_sun)/(R_p*R_sun)**2.			#Polar surface gravity
	#Populate the latitude/longitude grid
	R=np.zeros(len(colat))
	g=np.zeros(len(colat))
	g_r=np.zeros(len(colat))
	g_t=np.zeros(len(colat))
	lg=np.zeros(len(colat))
	#This defines the physical radius (in solar radii) and surface gravity of the star as a function of colatitude
	for i in range(len(colat)):
		if colat[i]==0.:
			R[i]=R_p
		elif colat[i]==np.pi:
			R[i]=R_p
		else:
			if lomg == 0.:
				R[i]=R_p
			else:
				R[i]=3.*R_p/(lomg*sin_colat[i])*np.cos((np.pi+np.arccos(lomg*sin_colat[i]))/3.)
		g_r[i]=-NG*(m*M_sun)/(R[i]*R_sun)**2.+R[i]*R_sun*(OMG*sin_colat[i])**2.
		g_t[i]=R[i]*R_sun*OMG**2.*sin_colat[i]*cos_colat[i]
		g[i]=np.sqrt(g_r[i]**2.+g_t[i]**2.)
		lg[i]=np.log10(g[i])
	#More calculations that need to be done
	tht_Re=(R_e*R_sun)/(dist*pc)	#Angular Equatorial Radius in radians
	tht_Rp=(R_p*R_sun)/(dist*pc)	#Angular Polar Radius in radians
	tht_R=(R*R_sun)/(dist*pc)	#Angular Radius as a function of colatitude in radians
	T_eff=T_p*(g/g_p)**beta	#Effective temperature as a function of colatitude in Kelvins
	
	#Setting up some arrays
	orig_x=np.zeros((len(colat),len(phi)))	#x coordinate before inclination and rotation
	orig_y=np.zeros((len(colat),len(phi)))	#y coordinate before inclination and rotation
	orig_z=np.zeros((len(colat),len(phi)))	#z coordinate before inclination and rotation
	mu=np.zeros((len(colat),len(phi)))	#cosine of the angle between the normal of the star and the observer
	#I don't think this is necessary anymore, but I don't have time to test right now:
	#emit=zeros(len(colat))		#The intensity emitted as a function of colatitude
	#int=zeros((len(colat),len(phi)))	#The intensity observed (before limb-darkening) as a function of colatitude and longitude
	
	#This bit calculates the x,y,z coordinates of each point on the grid as well as mu, the cosine of the angle between the normal of the star and the observer
	for i in range(len(colat)):
		A=tht_R[i]*sin_colat[i]
		B=tht_R[i]*cos_colat[i]
		C=tht_R[i]*sin_colat[i]
		for j in range(len(phi)):
			orig_x[i,j]=A*sin_phi[j]
			orig_y[i,j]=B
			orig_z[i,j]=C*cos_phi[j]
			mu[i,j]=1.0/g[i]*(-1.0*g_r[i]*(sin_colat[i]*sin_inc*cos_phi[j]+cos_colat[i]*cos_inc)-g_t[i]*(sin_inc*cos_phi[j]*cos_colat[i]-sin_colat[i]*cos_inc))
	#unrot_x,y,z have been inclined, but not rotated
	unrot_x=orig_x
	unrot_y=orig_y*sin_inc-orig_z*cos_inc
	unrot_z=orig_y*cos_inc+orig_z*sin_inc
	#x,y,z have been both inclined and rotated
	x=unrot_x*cos_pa-unrot_y*sin_pa
	y=unrot_x*sin_pa+unrot_y*cos_pa
	z=unrot_z
	#Defining above/below x,y,z's ("above" are points are the points the observer sees)
	x_above=[]
	y_above=[]
	z_above=[]
	#int_above=[]
	x_below=[]
	y_below=[]
	z_below=[]
	#int_below=[]
	for i in range(len(colat)):
		for j in range(len(phi)):
			if z[i,j] >= 0. and mu[i,j] > 0.:
				x_above.append(x[i,j])
				y_above.append(y[i,j])
				z_above.append(z[i,j])
				#int_above.append(int[i,j])
				#print 'i,j: {},{} || Colatitude: {} deg || Longitude: {} deg || x_above: {} mas || y_above: {} mas'.format(i,j,colat[i]*180./np.pi,phi[j]*180./np.pi,x[i,j]*206264806.,y[i,j]*206264806.)
			else:
				x_below.append(x[i,j])
				y_below.append(y[i,j])
				z_below.append(z[i,j])
				#int_below.append(int[i,j])
	#Define the perimeter of the "above" points
	points=[]
	for i in range(len(x_above)):
		points.append((x_above[i],y_above[i]))
	points=np.array(points)
	
	hull=ConvexHull(points)
	slist=[]
	xlist=[]
	ylist=[]
	for simplex in hull.simplices:
		slist.append(simplex)
		xlist.append(points[simplex,0])
		ylist.append(points[simplex,1])
	sarr=np.array(slist)
	xarr=np.array(xlist)
	yarr=np.array(ylist)
	perim_x,perim_y=sort_hull_results(sarr,xarr,yarr) #perim_x and perim_y define the perimeter of the star from the observer's perspective

	wl_list=[use_filts,filt_dict,uni_wl,uni_dwl]
	
	new_phx_dict,phx_mu,phx_wav,teff_list,logg_list,str_teff_list,str_logg_list=read_phoenix(phx_dir,mode,wl_list)
	if len(new_phx_dict) > len(phx_dict):
		phx_dict = new_phx_dict
	
	tg_lists=[teff_list,logg_list,str_teff_list,str_logg_list]
	
	g_points=0.
	

	if 'v' in mode:
		vis_chi2,g_points=calc_vis(p,R_p,beta,dist,lomg,OMG,m,vis,vis_err,wl,u_l,v_l,u_m,v_m,uni_wl,g_scale,perim_x,perim_y,n_params,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list,cal,y_above,x_above,star,model,model_dir)
	else:
		vis_chi2=0.
	if 'p' in mode:
		phot_chi2=calc_phot(p,R,tht_R,T_eff,g,g_r,g_t,lg,OMG,phot_data,colat,phi,sin_colat,cos_colat,cos_phi,sin_inc,cos_inc,filt_dict,use_filts,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,zpf,cwl,mode,wl_list,n_params,star,model,model_dir)
	else:
		phot_chi2=0.
	chi2=vis_chi2+phot_chi2
	
	extras=[0.,0.,0.,0.,0.,0.,0.,0.,0.,0.,0.,0.]
	'''
	g_re=-NG*(m*M_sun)/(R_e*R_sun)**2.+R_e*R_sun*(OMG*np.sin(np.pi/2.))**2.
	g_te=R_e*R_sun*OMG**2.*np.sin(np.pi/2.)*np.cos(np.pi/2.)
	g_e=np.sqrt(g_re**2.+g_te**2.)
	T_e=T_p*(g_e/g_p)**beta
	T_avg=np.trapz(T_eff,x=colat)/np.pi
	R_avg=np.trapz(R,x=colat)/np.pi
	g_avg=np.trapz(g,x=colat)/np.pi
	lg_p=np.log10(g_p)
	lg_e=np.log10(g_e)
	lg_avg=np.log10(g_avg)
	extras=[0.,0.,R_avg,R_p,T_e,T_avg,lg_p,lg_e,lg_avg,0.,0.,0.]
	'''
	
	if 'L' in mode:
		L_bol,L_app=calc_Lbol(p,R,tht_R,T_eff,g,g_r,g_t,lg,dist,phx_mu,colat,phi,sin_colat,cos_colat,cos_phi,sin_inc,cos_inc,phx_dir,use_Z,tg_lists,phx_dict,phx_wav,mode,wl_list)
		if 'a' in mode:
			mesa_dir='C:/Users/Jeremy/Dropbox/Python/Astars/MESA/History_Files/'
			age_guess=0.05
			
			#mesa_use_Z='Z0.016'	#UMa
			#fmasses=np.arange(19)*0.1+1.4
			#fomegas=np.arange(10)*0.1+0.0
			
			#mesa_use_Z='Z0.0153'	#New Solar
			#fmasses=np.arange(24)*0.1+1.0
			#fomegas=np.arange(10)*0.1+0.0

			#mesa_use_Z='Z0.0211'	#[M/H]=+0.14
			#fmasses=arange(24)*0.1+1.0
			#fomegas=arange(10)*0.1+0.0
			
			mesa_use_Z='Z0.0111'	#[M/H]=-0.14
			fmasses=np.arange(24)*0.1+1.0
			fomegas=np.arange(10)*0.1+0.0

			g_re=-NG*(m*M_sun)/(R_e*R_sun)**2.+R_e*R_sun*(OMG*np.sin(np.pi/2.))**2.
			g_te=R_e*R_sun*OMG**2.*np.sin(np.pi/2.)*np.cos(np.pi/2.)
			g_e=np.sqrt(g_re**2.+g_te**2.)
			T_e=T_p*(g_e/g_p)**beta
			T_avg=np.trapz(T_eff,x=colat)/np.pi
			R_avg=np.trapz(R,x=colat)/np.pi
			g_avg=np.trapz(g,x=colat)/np.pi
			lg_p=np.log10(g_p)
			lg_e=np.log10(g_e)
			lg_avg=np.log10(g_avg)
			#age,mass,omega=age_mass(L_bol,R_avg,vel,m,age_guess,lomg/2.,mesa_dir,mesa_use_Z,fmasses,fomegas,mode)
			age,mass,omega=age_mass(L_bol,R_avg,vel,m,age_guess,lomg,mesa_dir,mesa_use_Z,fmasses,fomegas,mode)
			extras=[L_bol,L_app,R_avg,R_p,T_e,T_avg,lg_p,lg_e,lg_avg,age,mass,omega]
			
	
	miniendtime=time.time()
	minielapsed=miniendtime-ministarttime
	
	if 'o' in mode:
		print 'Chi^2: {} (V: {}, P: {}). Params: [{}, {}, {}, {}, {}]. Time: {} s'.format(chi2,vis_chi2,phot_chi2,R_e,vel,inc*180./np.pi,T_p,pa*180./np.pi-90.,minielapsed)
	return chi2,phx_dict,g_points,extras
	#except:
	#	print 'An error occured. Returning with high chi^2.'
	#	return 1e8,phx_dict,0,[0.,0.,0.,0.,0.,0.,0.,0.,0.,0.,0.,0.]

def read_phoenix(phx_dir,mode,wl_list):
	"""Reads the phoenix model spectra and sets up phx_dict dictionary.
	
	Inputs:
	phx_dir
		The directory in which the phoenix model spectra are stored
		(this should already have the metallicity taken into account)
	mode
		Single letter string stating whether visibilities ('v'), photometry ('p'),
		or both ('b') are to be calculated.
	wl_list
		List of lists stating what wavelengths are to be used.
		If mode is 'p', wl_list will contain use_filts (the names of the filters
		used by the photometry) and filt_dict (the dictionary that contains
		the filter response curves). 
		If mode is 'v', wl_list will contain uni_wl (the unique wavelengths 
		associated with the observed visibilities) and uni_dwl (the 
		uncertainties associated with uni_wl).
		If mode is 'b', wl_list will contain use_filts, filt_dict, uni_wl, and uni_dwl 
		(in that order).		
	
	Outputs:
	phx_dict 
		A dictionary that has three data sets associated with each loaded phoenix model spectrum:
		unfiltered is a (78x25500) array of intensities as a function of mu and lambda.
		phot_filtered is a list of X lists which are each the intensity integrated over wavelength filter X.
		vis_filtered. same as phot_filtered, but appropriate for the visibilities instead of the photometry.
		If the mode doesn't call for either visibilities or photometry to be calculated, the respective foo_filtered
		list will be empty
	phx_mu
		An array of mu (the cosine of the angle between the normal and line of sight of the observer) values 
		used by the model phoenix spectra
	phx_wav
		An array of wavelength values used by the model phoenix spectra
	teff_list
		A list of the effective temperature values available in the phoenix model spectra
	logg_list
		A list of the log of surface gravity values available in the phoenix model spectra
	str_teff_list
		A list of the effective temperature values available in the phoenix model spectra as strings
	str_logg_list
		A list of the log of surface gravity values available in the phoenix model spectra as strings
	"""
	phx_file='Z-0.0/lte07000-4.00-0.0.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits' #An example file to get the dictionary started
	hdulist = pyfits.open(phx_dir+phx_file) #Read phx_file
	unfiltered = hdulist[0].data #The intensity array
	phx_mu=hdulist[1].data #The mu array
	hdulist.close()
	
	unfiltered=list(unfiltered)
	unfiltered=np.array(unfiltered)
	phx_mu=list(phx_mu) #I don't remember why I did this, but I feel like it was necessary
	phx_mu=np.array(phx_mu)
	phx_mu=np.concatenate((np.array([0.]),phx_mu))
	phx_wav=(np.arange(len(unfiltered[-1]))+500.)*1e-8 #This defines the wavelength array
	
	use_filts=wl_list[0]
	filt_dict=wl_list[1]
	uni_wl=wl_list[2]
	uni_dwl=wl_list[3]

	phot_filtered=[]
	if 'p' in mode:
		for i in range(len(use_filts)):
			inted=[]
			for j in np.arange(len(phx_mu)-1)+1:
				#non_inted is the filtered (i.e., had a filter response curve applied to it), non-integrated version of the intensity array 
				not_inted=unfiltered[j-1,:]*filt_dict[use_filts[i]]/fwhm(phx_wav,filt_dict[use_filts[i]])/1e8
				#not_inted_reduced is non_inted that only includes the non-zero bit of the sed
				#inted.append(np.trapz(not_inted,x=phx_wav))
				not_inted_reduced = not_inted[np.where(filt_dict[use_filts[i]] > 0)]
				#inted is non_inted_reduced that has been integrated over wavelength
				inted.append(do_phx_integrate(not_inted_reduced))
			phot_filtered.append(inted)
	
	vis_filtered=[]
	if 'v' in mode:
		for i in range(len(uni_wl)):
			inted=[]
			the_filter=np.zeros(len(phx_wav))
			for k in range(len(phx_wav)):
				#This creates a top-hat function for the filter response curve of the visibility observations
				if phx_wav[k]/100. > uni_wl[i]-uni_dwl[i]/2. and phx_wav[k]/100. <= uni_wl[i]+uni_dwl[i]/2.:
					the_filter[k]=1.
			for j in np.arange(len(phx_mu)-1)+1:
				#non_inted is the filtered (i.e., had a filter response curve applied to it), non-integrated version of the intensity array 
				not_inted=unfiltered[j-1,:]*the_filter/uni_dwl[i]
				#inted is non_inted that has been integrated over wavelength
				#inted.append(np.trapz(not_inted,x=phx_wav))
				not_inted_reduced = not_inted[np.where(not_inted > 0)]
				#inted is non_inted_reduced that has been integrated over wavelength
				inted.append(do_phx_integrate(not_inted_reduced))
			vis_filtered.append(inted)

	phx_dict={phx_file:[unfiltered,phot_filtered,vis_filtered]} #Create a dictionary with the data as a function of file name.
	
	#The rest of the function sets what models are available 
	str_teff_list=[]
	str_logg_list=[]
	
	teff_list_1=np.arange(27)*100+2300
	teff_list_2=np.arange(20)*100+5100
	teff_list_3=np.arange(25)*200+7200
	teff_list=np.concatenate((teff_list_1,teff_list_2,teff_list_3))

	for i in range(len(teff_list)):
		if teff_list[i] >= 10000:
			str_teff_list.append(str(teff_list[i]))
		else:
			str_teff_list.append('0'+str(teff_list[i]))
	logg_list=np.arange(13)*0.5
	str_logg_list=[]
	for i in range(len(logg_list)):
		if i == 0:
			str_logg_list.append('+'+str(logg_list[i])+'0')
		else:
			str_logg_list.append('-'+str(logg_list[i])+'0')
	return phx_dict,phx_mu,phx_wav,teff_list,logg_list,str_teff_list,str_logg_list

def extract_phoenix_full(teff,logg,mu,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,mode,wl_list):
	"""Constructs an intensity spectrum based on the effective temperature, surface gravity, and angle of observation
	
	Inputs:
	teff
		The effective temperature of the spectrum to be extracted
	logg
		The log of the surface gravity of the spectrum to be extracted
	mu
		The cosine of the angle between the normal and the line of sight of 
		the observation of the spectrum to be extracted	
	phx_dir
		The directory the phoenix spectra are located in.
	use_Z
		The metallicity used for desired phoenix model spectra. This is used in determining
		the ftp path for using  
	tg_lists
		A list of lists with teff_list, logg_list, str_teff_list, and str_logg_list
	phx_mu
		The list of mu values used by phoenix spectra
	phx_dict
		A dictionary with all the saved phoenix spectra in it
	
	Outputs:
	interpolated_flux
		An array with a spectrum for the given T_eff, log(g), and mu.
	
	"""
	
	#print teff,logg
	phx_dir=phx_dir+use_Z+'/'
	path_list=os.listdir(phx_dir)
	ftp_dir='ftp://phoenix.astro.physik.uni-goettingen.de/SpecIntFITS/PHOENIX-ACES-AGSS-COND-SPECINT-2011/'+use_Z+'/'
	
	teff_list=np.array(tg_lists[0])
	logg_list=np.array(tg_lists[1])
	str_teff_list=np.array(tg_lists[2])
	str_logg_list=np.array(tg_lists[3])
	phx_mu=np.array(phx_mu)
	
	gett=teff_list[np.where(teff_list >= round(teff,4))]
	lett=teff_list[np.where(teff_list <= round(teff,4))]
	tlo=max(lett)
	thi=min(gett)
	gegg=logg_list[np.where(logg_list >= round(logg,4))]
	legg=logg_list[np.where(logg_list <= round(logg,4))]
	glo=max(legg)
	ghi=min(gegg)
	gemm=phx_mu[np.where(phx_mu >= round(mu,4))]
	lemm=phx_mu[np.where(phx_mu <= round(mu,4))]
	if mu <1e-10:
		mlo=0.
		mlo_ind = 0
	else:
		mlo=max(lemm)
		mlo_ind=np.arange(len(phx_mu))[np.where(phx_mu == mlo)]
	mhi=min(gemm)
	mhi_ind=np.arange(len(phx_mu))[np.where(phx_mu == mhi)]

	tlo_str=str_teff_list[np.where(teff_list == tlo)][0]
	glo_str=str_logg_list[np.where(logg_list == glo)][0]
	thi_str=str_teff_list[np.where(teff_list == thi)][0]
	ghi_str=str_logg_list[np.where(logg_list == ghi)][0]

	#The four files representing the T_eff and log(g) on the grid just below and above what we want
	ll_file='lte'+tlo_str+glo_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	lh_file='lte'+tlo_str+ghi_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	hl_file='lte'+thi_str+glo_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	hh_file='lte'+thi_str+ghi_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'

	#Low Temperature, Low Gravity
	if ll_file in phx_dict:
		ll_arr = phx_dict[ll_file][0]
	elif ll_file in path_list:
		ll_arr=read_this_phoenix(ll_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[0]
	else:
		ll_arr=read_this_phoenix_ftp(ll_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[0]
	#Low Temperature, High Gravity
	if lh_file in phx_dict:
		lh_arr = phx_dict[lh_file][0]
	elif lh_file in path_list:
		lh_arr=read_this_phoenix(lh_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[0]
	else:
		lh_arr=read_this_phoenix_ftp(lh_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[0]
	#High Temperature, Low Gravity
	if hl_file in phx_dict:
		hl_arr = phx_dict[hl_file][0]
	elif hl_file in path_list:
		hl_arr=read_this_phoenix(hl_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[0]
	else:
		hl_arr=read_this_phoenix_ftp(hl_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[0]
	#High Temperature, High Gravity
	if hh_file in phx_dict:
		hh_arr = phx_dict[hh_file][0]
	elif hh_file in path_list:
		hh_arr=read_this_phoenix(hh_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[0]
	else:
		hh_arr=read_this_phoenix_ftp(hh_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[0]

	if mlo != mhi:
		if mlo == 0.:
			ll=ll_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			lh=lh_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			hl=hl_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			hh=hh_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			ll=ll[0]
			lh=lh[0]
			hl=hl[0]
			hh=hh[0]			
		else:
			ll=ll_arr[mlo_ind-1]+(ll_arr[mhi_ind-1]-ll_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			lh=lh_arr[mlo_ind-1]+(lh_arr[mhi_ind-1]-lh_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			hl=hl_arr[mlo_ind-1]+(hl_arr[mhi_ind-1]-hl_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			hh=hh_arr[mlo_ind-1]+(hh_arr[mhi_ind-1]-hh_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			ll=ll[0]
			lh=lh[0]
			hl=hl[0]
			hh=hh[0]
	else:
		if mlo == 0.:
			ll=np.zeros(len(phx_wav))
			lh=np.zeros(len(phx_wav))
			hl=np.zeros(len(phx_wav))
			hh=np.zeros(len(phx_wav))
		else:
			ll=ll_arr[mlo_ind-1]
			ll=ll[0]
			lh=lh_arr[mlo_ind-1]
			lh=lh[0]
			hl=hl_arr[mlo_ind-1]
			hl=hl[0]
			hh=hh_arr[mlo_ind-1]
			hh=hh[0]
	
	if thi != tlo and ghi != glo:
		interpolated_flux=ll/(thi-tlo)/(ghi-glo)*(thi-teff)*(ghi-logg)+lh/(thi-tlo)/(ghi-glo)*(thi-teff)*(logg-glo)+hl/(thi-tlo)/(ghi-glo)*(teff-tlo)*(ghi-logg)+hh/(thi-tlo)/(ghi-glo)*(teff-tlo)*(logg-glo)
	elif thi != tlo and ghi == glo:
		interpolated_flux=ll+(hl-ll)*(teff-tlo)/(thi-tlo)
	elif thi == tlo and ghi != glo:		
		interpolated_flux=ll+(lh-ll)*(logg-glo)/(ghi-glo)
	elif thi == tlo and ghi == glo:
		interpolated_flux=ll
	return np.array(interpolated_flux)
def extract_phoenix_phot(teff,logg,mu,filt,use_filts,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,mode,wl_list):
	"""Constructs an intensity spectrum based on the effective temperature, surface gravity, and angle of observation
	
	Inputs:
	teff
		The effective temperature of the spectrum to be extracted
	logg
		The log of the surface gravity of the spectrum to be extracted
	mu
		The cosine of the angle between the normal and the line of sight of 
		the observation of the spectrum to be extracted	
	filt
		The filter for which the photometry are to be extracted
	use_filts
		The list of all filters used for the photometry
	phx_dir
		The directory the phoenix spectra are located in.
	use_Z
		The metallicity used for desired phoenix model spectra. This is used in determining
		the ftp path for using  
	tg_lists
		A list of lists with teff_list, logg_list, str_teff_list, and str_logg_list
	phx_mu
		The list of mu values used by phoenix spectra
	phx_dict
		A dictionary with all the saved phoenix spectra in it
	
	Outputs:
	interpolated_flux
		An array with a spectrum for the given T_eff, log(g), and mu.
	
	"""
	
	filt_ind=np.arange(len(use_filts))[(np.array(use_filts) == filt).nonzero()][0]
	
	phx_dir=phx_dir+use_Z+'/'
	path_list=os.listdir(phx_dir)
	ftp_dir='ftp://phoenix.astro.physik.uni-goettingen.de/SpecIntFITS/PHOENIX-ACES-AGSS-COND-SPECINT-2011/'+use_Z+'/'
	
	teff_list=np.array(tg_lists[0])
	logg_list=np.array(tg_lists[1])
	str_teff_list=np.array(tg_lists[2])
	str_logg_list=np.array(tg_lists[3])
	phx_mu=np.array(phx_mu)
	
	gett=teff_list[np.where(teff_list >= round(teff,4))]
	lett=teff_list[np.where(teff_list <= round(teff,4))]
	tlo=max(lett)
	thi=min(gett)
	gegg=logg_list[np.where(logg_list >= round(logg,4))]
	legg=logg_list[np.where(logg_list <= round(logg,4))]
	glo=max(legg)
	ghi=min(gegg)
	gemm=phx_mu[np.where(phx_mu >= round(mu,4))]
	lemm=phx_mu[np.where(phx_mu <= round(mu,4))]
	if mu <1e-10:
		mlo=0.
		mlo_ind = 0
	else:
		mlo=max(lemm)
		mlo_ind=np.arange(len(phx_mu))[np.where(phx_mu == mlo)][0]
	mhi=min(gemm)
	mhi_ind=np.arange(len(phx_mu))[np.where(phx_mu == mhi)][0]

	tlo_str=str_teff_list[np.where(teff_list == tlo)][0]
	glo_str=str_logg_list[np.where(logg_list == glo)][0]
	thi_str=str_teff_list[np.where(teff_list == thi)][0]
	ghi_str=str_logg_list[np.where(logg_list == ghi)][0]

	#The four files representing the T_eff and log(g) on the grid just below and above what we want
	ll_file='lte'+tlo_str+glo_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	lh_file='lte'+tlo_str+ghi_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	hl_file='lte'+thi_str+glo_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	hh_file='lte'+thi_str+ghi_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	
	
	#Low Temperature, Low Gravity
	if ll_file in phx_dict:
		ll_arr = phx_dict[ll_file][1][filt_ind]
	elif ll_file in path_list:
		ll_arr=read_this_phoenix(ll_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	else:
		ll_arr=read_this_phoenix_ftp(ll_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	#Low Temperature, High Gravity
	if lh_file in phx_dict:
		lh_arr = phx_dict[lh_file][1][filt_ind]
	elif lh_file in path_list:
		lh_arr=read_this_phoenix(lh_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	else:
		lh_arr=read_this_phoenix_ftp(lh_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	#High Temperature, Low Gravity
	if hl_file in phx_dict:
		hl_arr = phx_dict[hl_file][1][filt_ind]
	elif hl_file in path_list:
		hl_arr=read_this_phoenix(hl_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	else:
		hl_arr=read_this_phoenix_ftp(hl_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	#High Temperature, High Gravity
	if hh_file in phx_dict:
		hh_arr = phx_dict[hh_file][1][filt_ind]
	elif hh_file in path_list:
		hh_arr=read_this_phoenix(hh_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	else:
		hh_arr=read_this_phoenix_ftp(hh_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[1][filt_ind]
	
	
	if mlo != mhi:
		if mlo == 0.:
			ll=ll_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			lh=lh_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			hl=hl_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			hh=hh_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
		else:
			ll=ll_arr[mlo_ind-1]+(ll_arr[mhi_ind-1]-ll_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			lh=lh_arr[mlo_ind-1]+(lh_arr[mhi_ind-1]-lh_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			hl=hl_arr[mlo_ind-1]+(hl_arr[mhi_ind-1]-hl_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			hh=hh_arr[mlo_ind-1]+(hh_arr[mhi_ind-1]-hh_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
	else:
		if mlo == 0.:
			ll=zeros(len(phx_wav))
			lh=zeros(len(phx_wav))
			hl=zeros(len(phx_wav))
			hh=zeros(len(phx_wav))
		else:
			ll=ll_arr[mlo_ind-1]
			lh=lh_arr[mlo_ind-1]
			hl=hl_arr[mlo_ind-1]
			hh=hh_arr[mlo_ind-1]
			
	if thi != tlo and ghi != glo:
		interpolated_flux=ll/(thi-tlo)/(ghi-glo)*(thi-teff)*(ghi-logg)+lh/(thi-tlo)/(ghi-glo)*(thi-teff)*(logg-glo)+hl/(thi-tlo)/(ghi-glo)*(teff-tlo)*(ghi-logg)+hh/(thi-tlo)/(ghi-glo)*(teff-tlo)*(logg-glo)
	elif thi != tlo and ghi == glo:
		interpolated_flux=ll+(hl-ll)*(teff-tlo)/(thi-tlo)
	elif thi == tlo and ghi != glo:		
		interpolated_flux=ll+(lh-ll)*(logg-glo)/(ghi-glo)
	elif thi == tlo and ghi == glo:
		interpolated_flux=ll
	
	return interpolated_flux

def extract_phoenix_vis(teff,logg,mu,wl_ind,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list):
	"""Constructs an intensity spectrum based on the effective temperature, surface gravity, and angle of observation
	
	Inputs:
	teff
		The effective temperature of the spectrum to be extracted
	logg
		The log of the surface gravity of the spectrum to be extracted
	mu
		The cosine of the angle between the normal and the line of sight of 
		the observation of the spectrum to be extracted	
	filt
		The filter for which the photometry are to be extracted
	wl_ind
		The index of the wavelength of the visibility measurement
	phx_dir
		The directory the phoenix spectra are located in.
	use_Z
		The metallicity used for desired phoenix model spectra. This is used in determining
		the ftp path for using  
	tg_lists
		A list of lists with teff_list, logg_list, str_teff_list, and str_logg_list
	phx_mu
		The list of mu values used by phoenix spectra
	phx_dict
		A dictionary with all the saved phoenix spectra in it
	
	Outputs:
	interpolated_flux
		An array with a spectrum for the given T_eff, log(g), and mu.
	
	"""
	phx_dir=phx_dir+use_Z+'/'
	path_list=os.listdir(phx_dir)
	ftp_dir='ftp://phoenix.astro.physik.uni-goettingen.de/SpecIntFITS/PHOENIX-ACES-AGSS-COND-SPECINT-2011/'+use_Z+'/'
	
	teff_list=np.array(tg_lists[0])
	logg_list=np.array(tg_lists[1])
	str_teff_list=np.array(tg_lists[2])
	str_logg_list=np.array(tg_lists[3])
	phx_mu=np.array(phx_mu)
	
	gett=teff_list[np.where(teff_list >= round(teff,4))]
	lett=teff_list[np.where(teff_list <= round(teff,4))]
	tlo=max(lett)
	thi=min(gett)
	gegg=logg_list[np.where(logg_list >= round(logg,4))]
	legg=logg_list[np.where(logg_list <= round(logg,4))]
	glo=max(legg)
	ghi=min(gegg)
	gemm=phx_mu[np.where(phx_mu >= round(mu,4))]
	lemm=phx_mu[np.where(phx_mu <= round(mu,4))]
	if mu <1e-10:
		mlo=0.
		mlo_ind = 0
	else:
		mlo=max(lemm)
		mlo_ind=np.arange(len(phx_mu))[np.where(phx_mu == mlo)][0]
	mhi=min(gemm)
	mhi_ind=np.arange(len(phx_mu))[np.where(phx_mu == mhi)][0]

	tlo_str=str_teff_list[np.where(teff_list == tlo)][0]
	glo_str=str_logg_list[np.where(logg_list == glo)][0]
	thi_str=str_teff_list[np.where(teff_list == thi)][0]
	ghi_str=str_logg_list[np.where(logg_list == ghi)][0]

	ll_file='lte'+tlo_str+glo_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	lh_file='lte'+tlo_str+ghi_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	hl_file='lte'+thi_str+glo_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'
	hh_file='lte'+thi_str+ghi_str+use_Z[1:]+'.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits'

	#Low Temperature, Low Gravity
	if ll_file in phx_dict:
		ll_arr = phx_dict[ll_file][2][wl_ind]
	elif ll_file in path_list:
		ll_arr=read_this_phoenix(ll_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	else:
		ll_arr=read_this_phoenix_ftp(ll_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	#Low Temperature, High Gravity
	if lh_file in phx_dict:
		lh_arr = phx_dict[lh_file][2][wl_ind]
	elif lh_file in path_list:
		lh_arr=read_this_phoenix(lh_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	else:
		lh_arr=read_this_phoenix_ftp(lh_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	#High Temperature, Low Gravity
	if hl_file in phx_dict:
		hl_arr = phx_dict[hl_file][2][wl_ind]
	elif hl_file in path_list:
		hl_arr=read_this_phoenix(hl_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	else:
		hl_arr=read_this_phoenix_ftp(hl_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	#High Temperature, High Gravity
	if hh_file in phx_dict:
		hh_arr = phx_dict[hh_file][2][wl_ind]
	elif hh_file in path_list:
		hh_arr=read_this_phoenix(hh_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]
	else:
		hh_arr=read_this_phoenix_ftp(hh_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list)[2][wl_ind]

	if mlo != mhi:
		if mlo == 0.:
			ll=ll_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			lh=lh_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			hl=hl_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
			hh=hh_arr[mhi_ind-1]*(mu-mlo)/(mhi-mlo)
		else:
			ll=ll_arr[mlo_ind-1]+(ll_arr[mhi_ind-1]-ll_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			lh=lh_arr[mlo_ind-1]+(lh_arr[mhi_ind-1]-lh_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			hl=hl_arr[mlo_ind-1]+(hl_arr[mhi_ind-1]-hl_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
			hh=hh_arr[mlo_ind-1]+(hh_arr[mhi_ind-1]-hh_arr[mlo_ind-1])*(mu-mlo)/(mhi-mlo)
	else:
		if mlo == 0.:
			ll=0.
			lh=0.
			hl=0.
			hh=0.
		else:
			ll=ll_arr[mlo_ind-1]
			lh=lh_arr[mlo_ind-1]
			hl=hl_arr[mlo_ind-1]
			hh=hh_arr[mlo_ind-1]
	
	if thi != tlo and ghi != glo:
		interpolated_flux=ll/(thi-tlo)/(ghi-glo)*(thi-teff)*(ghi-logg)+lh/(thi-tlo)/(ghi-glo)*(thi-teff)*(logg-glo)+hl/(thi-tlo)/(ghi-glo)*(teff-tlo)*(ghi-logg)+hh/(thi-tlo)/(ghi-glo)*(teff-tlo)*(logg-glo)
	elif thi != tlo and ghi == glo:
		interpolated_flux=ll+(hl-ll)*(teff-tlo)/(thi-tlo)
	elif thi == tlo and ghi != glo:		
		interpolated_flux=ll+(lh-ll)*(logg-glo)/(ghi-glo)
	elif thi == tlo and ghi == glo:
		interpolated_flux=ll
	return interpolated_flux

def unitrange(res):
	"""Outputs an array with values ranging from 0 to 1 with a number of elements given by the input.
	Input:
	res
		The number of elements you want in the array
	Output:
	return
		A numpy array that ranges from 0 to 1 with res elements
	"""
	return np.arange(res+1)/float(res)
def fwhm(wave,transmission):
	"""Determines the full width half max of the supplied transmission curve
	Inputs:
	wave
		An array of wavelengths
	transmission
		The fractional transmission of the filter curve at wavelengths 'wave'
	Output:
	fwhm
		The full width half max of the transmission curve
	"""
	#The following test to see if the transmission curve is a top hat function rather 
	#than a normal transmission curve
	test_trans=transmission[np.where(transmission > 0.01)]	#An array of all the nonzero points in transmission
	test_trans=test_trans[np.where(test_trans < 0.99)]		#An array of all the nonzero/non-one points in the transmission
	if len(test_trans) == 0:	#If the tranmsission array is only zeros and ones
		ones_wave=wave[np.where(transmission > 0.01)]
		fwhm_high=max(ones_wave)
		fwhm_low=min(ones_wave)
		fwhm=fwhm_high-fwhm_low	#The fwhm is the difference between where the ones end and where they begin
	else:	#If the transmission array isn't a top hat function
		close_to_one=min((transmission-1.)**2.)	#I'm not sure why I did it this way... I should double check this later
		max_wave=wave[np.where((transmission-1.)**2.==close_to_one)]
		max_wave=max_wave[len(max_wave)/2]
		wave_high=wave[np.where(wave > max_wave)]
		trans_high=transmission[np.where(wave > max_wave)]
		close_to_half=min((trans_high-0.5)**2.)
		fwhm_high=wave_high[np.where((trans_high-0.5)**2.==close_to_half)]
		wave_low=wave[np.where(wave < max_wave)]
		trans_low=transmission[np.where(wave < max_wave)]
		close_to_half=min((trans_low-0.5)**2.)
		fwhm_low=wave_low[np.where((trans_low-0.5)**2.==close_to_half)]
		fwhm=fwhm_high-fwhm_low	
	return fwhm

def read_filters(use_filts,filt_dir,cwl,wav):
	"""Reads and stores the data from the filter curves to be used.
	Inputs:
	use_filts
		A list of the names of the filters used by the photometry
	filt_dir
		The directory in which the filter curves are stored
	cwl
		A dictionary with the values for the central wavelengths
	wav
		An array of wavelength values used by the model spectra
	Outputs:
	filt_dict
		A dictionary with the filter transmission curve expressed where 
		the corresponding wavelength array is that being used for the SED
	"""
	filt_dict=dict()
	for i in range(len(use_filts)):
		this_wav=[]
		this_tran=[]
		with open(filt_dir+use_filts[i]+'.txt') as input:	#This opens and records the wavelength and transmission vectors
			try:
				input_reader=csv.reader(input,delimiter='\t')	#If the data are tab-delimited
				input_reader.next()
				for line in input_reader:
					this_wav.append(float(line[0])/1e8)
					this_tran.append(float(line[1]))
			except:
				input_reader=csv.reader(input,delimiter=' ')	#If the data are space-delimited
				input_reader.next()
				for line in input_reader:
					this_wav.append(float(line[0])/1e8)
					this_tran.append(float(line[1]))
		int_trans=np.zeros(len(wav))
		f=interp1d(this_wav,this_tran)	#This interpolates the saved filter curve to the appropriate wavelength scale
		for j in range(len(wav)):
			if wav[j] >= this_wav[0] and wav[j] <= this_wav[-1]:
				int_trans[j]=f(wav[j])
		abs_wav_minus_cwl=abs(wav - cwl[use_filts[i]])
		cwl_trans=int_trans[np.where(abs_wav_minus_cwl == min(abs_wav_minus_cwl))]
		int_trans=int_trans/cwl_trans[0]
		filt_dict[use_filts[i]]=int_trans
	return filt_dict
		
		
def sort_hull_results(sarr,xarr,yarr):
	"""Takes the results from ConvexHull and puts them into two, easy-to-use 1D arrays for x/y coordinates
	Inputs
	sarr
		Xx2 array of simplices
	xarr
		Xx2 array of x-coordinates
	yarr
		Xx2 array of y-coordinates
	Outputs:
	xpts
		1D arrays of x-coordinates
	ypts
		1D arrays of y-coordinates
	"""
	spts=[]
	xpts=[]
	ypts=[]
	spts.append(sarr[0][0])
	xpts.append(xarr[0][0])
	ypts.append(yarr[0][0])
	spts.append(sarr[0][1])
	xpts.append(xarr[0][1])
	ypts.append(yarr[0][1])


	while len(spts) < np.shape(sarr)[0]:
		for i in np.arange(np.shape(sarr)[0]-1)+1:
			if sarr[i][0] not in spts or sarr[i][1] not in spts:
				if sarr[i][0]==spts[-1]:
					spts.append(sarr[i][1])
					xpts.append(xarr[i][1])
					ypts.append(yarr[i][1])
				elif sarr[i][1]==spts[-1]:
					spts.append(sarr[i][0])
					xpts.append(xarr[i][0])
					ypts.append(yarr[i][0])
	spts.append(spts[0])
	xpts.append(xpts[0])
	ypts.append(ypts[0])
	return np.array(xpts),np.array(ypts)	
	
def extract(tht_x,tht_y,inc,R_e,T_p,beta,R_p,dist,lomg,OMG,m,pa,index,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list):
	"""Calculates the intensity of the image at the supplied x,y coordinates.
	Inputs:
	tht_x
		x coordinate of interest in radians
	tht_y
		y coordinate of interest in radians
	inc
		The inclination of the model star
	pa
		The position angle of the model star
	R_e
		The equatorial radius of the model star
	R_p
		The polar radius of the model star
	lomg
		The fraction angular velocity (relative to critical) of the model star
	index
		The index of uni_wl for the image
	phx_dir
		The directory the phoenix spectra are located in.
	use_Z
		The metallicity used for desired phoenix model spectra. This is used in determining
		the ftp path for using  
	tg_lists
		A list of lists with teff_list, logg_list, str_teff_list, and str_logg_list
	phx_mu
		The list of mu values used by phoenix spectra
	phx_dict
		A dictionary with all the saved phoenix spectra in it
	
	Outputs:
	intensity
		A float of the intensity at the given x,y coordinates
	"""
	#Convert x and y into solar units (from radians)
	x=tht_x*dist*pc/R_sun
	y=tht_y*dist*pc/R_sun
	#Start with an assumption for z such that star is a sphere
	zass=np.sqrt(R_e**2.-x**2.-y**2.)
	R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)	#Convert x,y, and zass from cartesian to spherical coordinates
	
	if inc==0 and x==0 and y==0:
		R_tht=R_p
	else:
		R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		
		#If the radius you expect from (x,y,zass) doesn't match the radius you expect from the tht and phi that
		#	are associated with x,y,zass, then zass gets changed until they do match to within 0.1%.
		if abs(R_tht-R_xyz)/R_tht*100. > 0.1:
			while zass > 0 and R_tht < R_xyz:
				zass-=5e-2*R_e
				R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
				R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
			zass+=5e-2*R_e
			R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
			R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		if abs(R_tht-R_xyz)/R_tht*100. > 0.1:
			while zass > 0 and R_tht < R_xyz:
				zass-=1e-2*R_e
				R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
				R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
			zass+=1e-2*R_e
			R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
			R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		if abs(R_tht-R_xyz)/R_tht*100. > 0.1:
			while zass > 0 and R_tht < R_xyz:
				zass-=1e-3*R_e
				R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
				R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
			zass+=1e-3*R_e
			R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
			R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		if abs(R_tht-R_xyz)/R_tht*100. > 0.1:
			while zass > 0 and R_tht < R_xyz:
				zass-=1e-4*R_e
				R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
				R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
			zass+=1e-4*R_e
			R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
			R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		if abs(R_tht-R_xyz)/R_tht*100. > 0.1:
			while zass > 0 and R_tht < R_xyz:
				zass-=1e-5*R_e
				R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
				R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
			zass+=1e-5*R_e
			R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
			R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		if abs(R_tht-R_xyz)/R_tht*100. > 0.1:
			while zass > 0 and R_tht < R_xyz:
				zass-=1e-6*R_e
				R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
				R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
			zass+=1e-6*R_e
			R_xyz,tht_xyz,phi_xyz=cart2sphere(x,y,zass,inc,pa)
			R_tht=3.*R_p/(lomg*np.sin(tht_xyz))*np.cos((np.pi+np.arccos(lomg*np.sin(tht_xyz)))/3.) #Radius as a function of colatitude
		
	g_t=R_xyz*R_sun*OMG**2.*np.sin(tht_xyz)*np.cos(tht_xyz)
	g_r=-NG*(m*M_sun)/(R_xyz*R_sun)**2.+R_xyz*R_sun*(OMG*np.sin(tht_xyz))**2.
	g=np.sqrt(g_r**2.+g_t**2.)
	mu=1.0/g*(-1.0*g_r*(np.sin(tht_xyz)*np.sin(inc)*np.cos(phi_xyz)+np.cos(tht_xyz)*np.cos(inc))-g_t*(np.sin(inc)*np.cos(phi_xyz)*np.cos(tht_xyz)-np.sin(tht_xyz)*np.cos(inc)))
	g_p=NG*(m*M_sun)/(R_p*R_sun)**2.			#Polar surface gravity
	T_eff=T_p*(g/g_p)**beta	#Effective temperature as a function of colatitude in Kelvins
	
	intensity=extract_phoenix_vis(T_eff,np.log10(g),mu,index,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list)
	return intensity	



def cart2sphere(xxx,yyy,zzz,inc,pa):
	"""Converts the input cartesian coordinates into spherical coordinates (adjusting for inclination and position angle of the star)
	Inputs:
	xxx
		The x coordinate to be converted
	yyy
		The y coordinate to be converted
	zzz
		The z coordinate to be converted
	inc
		The inclination of the model star
	pa
		The position angle of the model star
	
	Outputs:
	r
		The associated r coordinate
	tht
		The associated tht coordinate
	phi
		The associated phi coordinate
	"""
	#Rotating by the position angle
	xx=xxx*np.cos(pa)+yyy*np.sin(pa)
	yy=-xxx*np.sin(pa)+yyy*np.cos(pa)
	zz=zzz
	#Rotating by the inclination
	x=xx
	y=yy*np.sin(inc)+zz*np.cos(inc)
	z=-yy*np.cos(inc)+zz*np.sin(inc)
	#Converting to spherical coordinates
	r=np.sqrt(x**2.+y**2.+z**2.)
	phi=np.arccos(z/np.sqrt(z**2.+x**2.))
	tht=np.arccos(y/np.sqrt(z**2.+x**2.+y**2.))
	return r,tht,phi
	
def calc_beta(omg):
	"""Calculates the gravity darkening coefficient, beta based on the fractional rotation velocity, omg based on the ELR_2011 method
	This function (as well as the ftht and froche functions) is derived from IDL code sent to me by Michel Rieutord.
	Inputs:
	omg
		The fraction angular velocity (relative to critical) of the model star
	
	Outputs:
	beta_exp
		The gravity darkening coefficient
	"""
	from scipy.optimize import *
	global cftht,omm_c,cth,lntg,rt,it
	global comc,om_c,sth2
	
	if omg == 0.:
		return 0.25,0.0
		
	omk=np.sqrt(6./omg * np.sin(np.arcsin(omg)/3.) - 2.)
	
	it=0
	flat=1.-1./(1.+omk**2./2.)
	beta_exp=0.
	betamin=0.
	betamax=0.
	
	om_c=omk
	omm_c=om_c
	
	num_pts=50
	theta=np.zeros(num_pts)
	rtild=np.zeros(num_pts)
	tht=np.zeros(num_pts)
	theta=np.pi*np.arange(num_pts)/(num_pts-1)/2.
	for i in range(num_pts):
		sth2=np.sin(theta[i])**2.
		xs=[max([0.5,rtild[i]]),1.,1.0001]
		rt=fsolve(froche,xs,xtol=1e-10)
		rtild[i]=rt[0]
	for i in np.arange(num_pts-2)+1:
		cth=np.cos(theta[i])
		lntg=np.log(np.tan(theta[i]/2.))
		rt=rtild[i]
		x=[0.001,theta[i]/2.+0.001,theta[i]/2.+0.02]
		f_tht=fsolve(ftht,x,xtol=1e-10)
		tht[i]=f_tht[0]
	tht[num_pts-1]=np.pi/2.
	
	fl1=np.sqrt(1./rtild**4.+np.sin(theta)**2.*(om_c**4.*rtild**2.-2.*om_c**2./rtild))
	flux=np.zeros(num_pts)
	for i in range(num_pts):
		if i == 0:
			flux[i]=np.sqrt(1./rtild[0]**4.)*np.exp(2.*om_c**2.*rtild[0]**3./3.)
		elif i == num_pts-1:
			flux[i]=np.sqrt(1./rtild[num_pts-1]**4.+(om_c**4.*rtild[num_pts-1]**2.-2.*om_c**2./rtild[num_pts-1]))/(1.-om_c**2.*rtild[num_pts-1]**3.)**(2./3.)
		else:
			flux[i]=(np.tan(tht[i])/np.tan(theta[i]))**2*fl1[i]
	beta_exp=np.log(flux[num_pts-1]/flux[0])/np.log(fl1[num_pts-1]/fl1[0])/4.
	return beta_exp

def ftht(tht):
	"""	This function (as well as the calc_beta and froche functions) is derived from IDL code sent to me by Michel Rieutord.
	"""
	global cftht,omm_c,cth,lntg,rt,it
	if it == 1:
		print 'in ftht',om,cth,lntg,rt,it
	ftht=np.cos(tht)+np.log(np.tan(tht/2.))-cth-lntg-omm_c**2.*rt**3.*cth**3./3.
	return ftht

def froche(r):
	"""	This function (as well as the calc_beta and ftht functions) is derived from IDL code sent to me by Michel Rieutord.
	"""
	global comc,om_c,sth2
	froche=1./om_c**2.*(1./r-1.)+0.5*(r**2.*sth2-1.)
	return froche
def age_mass(in_lum,in_rad,in_vel,gm,ga,gw,mesa_dir,mesa_use_Z,fmasses,fomegas,mode):
	"""Determines the age, mass, and initial rotation rate for the given luminosity, radius, and equatorial velocity.
	Inputs:
	in_lum
		The luminosity of the model star
	in_rad
		The average radius of the model star
	in_vel
		The equatorial velocity of the model star
	gm
		The initial guess at the mass
	ga
		The initial guess at the age
	gw
		The initial guess at the initial rotation rate
	mesa_use_Z
		The internal metallicity to be used
	mesa_dir
		The directory in which the mesa output files are stored
	fmasses
		An array of floats representing the masses available for the given mesa_use_Z
	fomegas
		An array of floats representing the omegas available for the given mesa_use_Z
	"""
	
	#print 'Using metallicity: {}'.format(mesa_use_Z)
	
	masses=[]
	omegas=[]
	for i in range(len(fmasses)):
		masses.append(str(fmasses[i]))
	for i in range(len(fomegas)):
		omegas.append(str(fomegas[i]))
	mesa_dict=read_mesa(masses,omegas,mesa_dir,mesa_use_Z)
	
	start_params=[gm,ga,gw]
	params=[gm,ga,gw]
	prange=np.array([0.3,0.3,0.3])
	base_gof=match(params,[in_lum,in_rad,in_vel,fmasses,fomegas,mesa_dict,mesa_dir,mesa_use_Z])
	if 'o' in mode:
		print 'Starting parameters: GoF: {}, Mass: {}, Age: {}, Omg: {}'.format(1./base_gof,params[0],params[1],params[2])
	base_gof=match(params,[in_lum,in_rad,in_vel,fmasses,fomegas,mesa_dict,mesa_dir,mesa_use_Z])
	
	gof=1./base_gof
	i=0
	k=0
	ii=0
	reset=True
	while gof > 1e-7:
		params=amoeba(params,prange,match,ftolerance=1.e-10,xtolerance=1.e-10,itmax=1000,data=[in_lum,in_rad,in_vel,fmasses,fomegas,mesa_dict,mesa_dir,mesa_use_Z])
		gof=1./params[1]
		if gof < 1e-7:
			reset=False
		params=params[0]
		if 'o' in mode:
			print '{} iterations till timeout. Parameters: GoF: {}, Mass: {}, Age: {}, Omg: {}'.format(300-ii,gof,params[0],params[1],params[2])
		i+=1
		ii+=1
		if i == 5 and reset == True:
			if k == 10:
				params[0]=start_params[0]
				params[1]=start_params[1]
				params[2]=start_params[2]
				if 'o' in mode:
					print 'Resetting Mass, Age, and Omg'
				k=0
			else:
				j=random.randint(1,6)
				if j == 1:
					params[0]-=0.1
					if 'o' in mode:
						print 'Adjusting Mass down' 
					k+=1
				if j == 2:
					params[0]+=0.1
					if 'o' in mode:
						print 'Adjusting Mass up' 
					k+=1
				if j == 3:
					params[1]-=0.1
					if 'o' in mode:
						print 'Adjusting Age down' 
					k+=1
				if j == 4:
					params[1]+=0.1
					if 'o' in mode:
						print 'Adjusting Age up' 
					k+=1
				if j == 5:
					params[2]-=0.1
					if 'o' in mode:
						print 'Adjusting Omg down' 
					k+=1
				if j == 6:
					params[2]+=0.1
					if 'o' in mode:
						print 'Adjusting Omg up' 
					k+=1
			i = 0
		if ii == 300:
			print 'age_mass failure (took too long). Returning mass, age, omg as [0.,0.,0.]'
			return 0.,0.,0.
	if 'o' in mode:
		print 'Final Parameters: Mass: {}, Age: {}, Omg: {}'.format(params[0],params[1],params[2])
		print '==============================================='
	return params[1],params[0],params[2]
def match(p,data):
	"""Determines how close the given mass, age, and initial rotation velocity comes to matching the given luminosity, radius, and current rotation velocity
	
	Inputs:
	p
		The list of variables to be altered by the amoeba function.
		It includes the mass (p[0]), the age (p[1]) and the initial rotation velocity (p[2]) to be tested.
	data
		The list of items that need to be passed on to the function. What data includes is listed below.
	in_lum
		The luminosity of the model star
	in_rad
		The average radius of the model star
	in_vel
		The equatorial velocity of the model star
	fmasses
		An array of floats representing the masses available for the given mesa_use_Z
	fomegas
		An array of floats representing the omegas available for the given mesa_use_Z
	mesa_dict
		The dictionary of mass tracks stored for the given fmasses/fomegas combo
	mesa_dir
		The directory in which the mesa output files are stored
	mesa_use_Z
		The internal metallicity to be used
		
	Outputs:
	1./gof
		gof is the goodness of fit parameter used to determine how close the modeled L, R, and Ve are
		to those calculated for the input m, a, and w. 1./gof is used because amoeba maximizes the returned
		value and we want to minimize gof.
	"""
	
	in_lum,in_rad,in_vel,fmasses,fomegas,mesa_dict,mesa_dir,mesa_use_Z=data
	try:
		#print p
		mass=p[0]
		age=p[1]
		omg=p[2]
		gem=fmasses[np.where(fmasses >= round(mass,4))]
		lem=fmasses[np.where(fmasses <= round(mass,4))]
		m_lo=max(lem)
		m_hi=min(gem)
		gew=fomegas[np.where(fomegas >= round(omg,4))]
		lew=fomegas[np.where(fomegas <= round(omg,4))]
		w_lo=max(lew)
		w_hi=min(gew)
		#The age, luminosity, polar radius, and equatorial velocity for the low mass, low omega case
		age_ll=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_lo)][0]
		lum_ll=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_lo)][2]
		rad_ll=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_lo)][3]
		rp_ll=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_lo)][4]
		vel_ll=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_lo)][5]
		wnow_ll=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_lo)][6]
		#Determine the low and high age for this set
		gea_ll=age_ll[np.where(age_ll >=age)]
		lea_ll=age_ll[np.where(age_ll <=age)]
		age_lll=max(lea_ll)
		age_llh=min(gea_ll)
		lum_lll=lum_ll[np.where(age_ll ==age_lll)][0]
		lum_llh=lum_ll[np.where(age_ll == age_llh)][0]
		rad_lll=rad_ll[np.where(age_ll == age_lll)][0]
		rad_llh=rad_ll[np.where(age_ll == age_llh)][0]
		rp_lll=rp_ll[np.where(age_ll == age_lll)][0]
		rp_llh=rp_ll[np.where(age_ll == age_llh)][0]
		vel_lll=vel_ll[np.where(age_ll == age_lll)][0]
		vel_llh=vel_ll[np.where(age_ll == age_llh)][0]
		wnow_lll=wnow_ll[np.where(age_ll == age_lll)][0]
		wnow_llh=wnow_ll[np.where(age_ll == age_llh)][0]
		#The age, luminosity, polar radius, and equatorial velocity for the low mass, high omega case
		age_lh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_hi)][0]
		lum_lh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_hi)][2]
		rad_lh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_hi)][3]
		rp_lh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_hi)][4]
		vel_lh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_hi)][5]
		wnow_lh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_lo)+'_w'+str(w_hi)][6]
		#Determine the low and high age for this set
		gea_lh=age_lh[np.where(age_lh >=age)]
		lea_lh=age_lh[np.where(age_lh <=age)]
		age_lhl=max(lea_lh)
		age_lhh=min(gea_lh)
		lum_lhl=lum_lh[np.where(age_lh == age_lhl)][0]
		lum_lhh=lum_lh[np.where(age_lh == age_lhh)][0]
		rad_lhl=rad_lh[np.where(age_lh == age_lhl)][0]
		rad_lhh=rad_lh[np.where(age_lh == age_lhh)][0]
		rp_lhl=rp_lh[np.where(age_lh == age_lhl)][0]
		rp_lhh=rp_lh[np.where(age_lh == age_lhh)][0]
		vel_lhl=vel_lh[np.where(age_lh == age_lhl)][0]
		vel_lhh=vel_lh[np.where(age_lh == age_lhh)][0]
		wnow_lhl=wnow_lh[np.where(age_lh == age_lhl)][0]
		wnow_lhh=wnow_lh[np.where(age_lh == age_lhh)][0]
		#The age, luminosity, polar radius, and equatorial velocity for the high mass, low omega case
		age_hl=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_lo)][0]
		lum_hl=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_lo)][2]
		rad_hl=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_lo)][3]
		rp_hl=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_lo)][4]
		vel_hl=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_lo)][5]
		wnow_hl=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_lo)][6]
		#Determine the low and high age for this set
		gea_hl=age_hl[np.where(age_hl >=age)]
		lea_hl=age_hl[np.where(age_hl <=age)]
		age_hll=max(lea_hl)
		age_hlh=min(gea_hl)
		lum_hll=lum_hl[np.where(age_hl == age_hll)][0]
		lum_hlh=lum_hl[np.where(age_hl == age_hlh)][0]
		rad_hll=rad_hl[np.where(age_hl == age_hll)][0]
		rad_hlh=rad_hl[np.where(age_hl == age_hlh)][0]
		rp_hll=rp_hl[np.where(age_hl == age_hll)][0]
		rp_hlh=rp_hl[np.where(age_hl == age_hlh)][0]
		vel_hll=vel_hl[np.where(age_hl == age_hll)][0]
		vel_hlh=vel_hl[np.where(age_hl == age_hlh)][0]
		wnow_hll=wnow_hl[np.where(age_hl == age_hll)][0]
		wnow_hlh=wnow_hl[np.where(age_hl == age_hlh)][0]
		#The age, luminosity, polar radius, and equatorial velocity for the high mass, high omega case
		age_hh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_hi)][0]
		lum_hh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_hi)][2]
		rad_hh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_hi)][3]
		rp_hh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_hi)][4]
		vel_hh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_hi)][5]
		wnow_hh=mesa_dict[mesa_dir+mesa_use_Z+'_M'+str(m_hi)+'_w'+str(w_hi)][6]
		#Determine the low and high age for this set
		gea_hh=age_hh[np.where(age_hh >=age)]
		lea_hh=age_hh[np.where(age_hh <=age)]
		age_hhl=max(lea_hh)
		age_hhh=min(gea_hh)
		lum_hhl=lum_hh[np.where(age_hh == age_hhl)][0]
		lum_hhh=lum_hh[np.where(age_hh == age_hhh)][0]
		rad_hhl=rad_hh[np.where(age_hh == age_hhl)][0]
		rad_hhh=rad_hh[np.where(age_hh == age_hhh)][0]
		rp_hhl=rp_hh[np.where(age_hh == age_hhl)][0]
		rp_hhh=rp_hh[np.where(age_hh == age_hhh)][0]
		vel_hhl=vel_hh[np.where(age_hh == age_hhl)][0]
		vel_hhh=vel_hh[np.where(age_hh == age_hhh)][0]
		wnow_hhl=wnow_hh[np.where(age_hh == age_hhl)][0]
		wnow_hhh=wnow_hh[np.where(age_hh == age_hhh)][0]
		
		#Interpolate over age
		if age_llh != age_lll:
			#Luminosity, Polar Radius, and Equatorial Velocity of the low mass, low omega case
			lum_ll=lum_lll+(lum_llh-lum_lll)*(age-age_lll)/(age_llh-age_lll)
			rad_ll=rad_lll+(rad_llh-rad_lll)*(age-age_lll)/(age_llh-age_lll)
			rp_ll=rp_lll+(rp_llh-rp_lll)*(age-age_lll)/(age_llh-age_lll)
			vel_ll=vel_lll+(vel_llh-vel_lll)*(age-age_lll)/(age_llh-age_lll)
			wnow_ll=wnow_lll+(wnow_llh-wnow_lll)*(age-age_lll)/(age_llh-age_lll)
		else:
			lum_ll=lum_lll
			rad_ll=rad_lll
			rp_ll=rp_lll
			vel_ll=vel_lll
			wnow_ll=wnow_lll
		if age_lhh != age_lhl:
			#Luminosity, Polar Radius, and Equatorial Velocity of the low mass, high omega case
			lum_lh=lum_lhl+(lum_lhh-lum_lhl)*(age-age_lhl)/(age_lhh-age_lhl)
			rad_lh=rad_lhl+(rad_lhh-rad_lhl)*(age-age_lhl)/(age_lhh-age_lhl)
			rp_lh=rp_lhl+(rp_lhh-rp_lhl)*(age-age_lhl)/(age_lhh-age_lhl)
			vel_lh=vel_lhl+(vel_lhh-vel_lhl)*(age-age_lhl)/(age_lhh-age_lhl)
			wnow_lh=wnow_lhl+(wnow_lhh-wnow_lhl)*(age-age_lhl)/(age_lhh-age_lhl)
		else:
			lum_lh=lum_lhl
			rad_lh=rad_lhl
			rp_lh=rp_lhl
			vel_lh=vel_lhl
			wnow_lh=wnow_lhl
		if age_hlh != age_hll:
			#Luminosity, Polar Radius, and Equatorial Velocity of the high mass, low omega case
			lum_hl=lum_hll+(lum_hlh-lum_hll)*(age-age_hll)/(age_hlh-age_hll)
			rad_hl=rad_hll+(rad_hlh-rad_hll)*(age-age_hll)/(age_hlh-age_hll)
			rp_hl=rp_hll+(rp_hlh-rp_hll)*(age-age_hll)/(age_hlh-age_hll)
			vel_hl=vel_hll+(vel_hlh-vel_hll)*(age-age_hll)/(age_hlh-age_hll)
			wnow_hl=wnow_hll+(wnow_hlh-wnow_hll)*(age-age_hll)/(age_hlh-age_hll)
		else:
			lum_hl=lum_hll
			rad_hl=rad_hll
			rp_hl=rp_hll
			vel_hl=vel_hll
			wnow_hl=wnow_hll
		if age_hhh != age_hhl:
			#Luminosity, Polar Radius, and Equatorial Velocity of the high mass, high omega case
			lum_hh=lum_hhl+(lum_hhh-lum_hhl)*(age-age_hhl)/(age_hhh-age_hhl)
			rad_hh=rad_hhl+(rad_hhh-rad_hhl)*(age-age_hhl)/(age_hhh-age_hhl)
			rp_hh=rp_hhl+(rp_hhh-rp_hhl)*(age-age_hhl)/(age_hhh-age_hhl)
			vel_hh=vel_hhl+(vel_hhh-vel_hhl)*(age-age_hhl)/(age_hhh-age_hhl)
			wnow_hh=wnow_hhl+(wnow_hhh-wnow_hhl)*(age-age_hhl)/(age_hhh-age_hhl)
		else:
			lum_hh=lum_hhl
			rad_hh=rad_hhl
			rp_hh=rp_hhl
			vel_hh=vel_hhl
			wnow_hh=wnow_hhl
	
		#Interpolate over omega
		
		if w_hi != w_lo:
			#Luminosity, Polar Radius, and Equatorial Velocity of the low mass case
			lum_l=lum_ll+(lum_lh-lum_ll)*(omg-w_lo)/(w_hi-w_lo)
			rad_l=rad_ll+(rad_lh-rad_ll)*(omg-w_lo)/(w_hi-w_lo)
			rp_l=rp_ll+(rp_lh-rp_ll)*(omg-w_lo)/(w_hi-w_lo)
			vel_l=vel_ll+(vel_lh-vel_ll)*(omg-w_lo)/(w_hi-w_lo)
			wnow_l=wnow_ll+(wnow_lh-wnow_ll)*(omg-w_lo)/(w_hi-w_lo)
			#Luminosity, Polar Radius, and Equatorial Velocity of the high mass case
			lum_h=lum_hl+(lum_hh-lum_hl)*(omg-w_lo)/(w_hi-w_lo)
			rad_h=rad_hl+(rad_hh-rad_hl)*(omg-w_lo)/(w_hi-w_lo)
			rp_h=rp_hl+(rp_hh-rp_hl)*(omg-w_lo)/(w_hi-w_lo)
			vel_h=vel_hl+(vel_hh-vel_hl)*(omg-w_lo)/(w_hi-w_lo)
			wnow_h=wnow_hl+(wnow_hh-wnow_hl)*(omg-w_lo)/(w_hi-w_lo)
		else:
			lum_l=lum_ll
			rad_l=rad_ll
			rp_l=rp_ll
			vel_l=vel_ll
			wnow_l=wnow_ll
			lum_h=lum_hl
			rad_h=rad_hl
			rp_h=rp_hl
			vel_h=vel_hl
			wnow_h=wnow_hl
		
		#Interpolate over mass
		
		if m_hi != m_lo:
			lum=lum_l+(lum_h-lum_l)*(mass-m_lo)/(m_hi-m_lo)
			rad=rad_l+(rad_h-rad_l)*(mass-m_lo)/(m_hi-m_lo)
			rp=rp_l+(rp_h-rp_l)*(mass-m_lo)/(m_hi-m_lo)
			#print m_lo,mass,m_hi
			#print rp_l,rp,rp_h
			vel=vel_l+(vel_h-vel_l)*(mass-m_lo)/(m_hi-m_lo)
			wnow=wnow_l+(wnow_h-wnow_l)*(mass-m_lo)/(m_hi-m_lo)
		else:
			lum=lum_l
			rad=rad_l
			rp=rp_l
			#print m_lo,mass,m_hi
			#print rp_l,rp,rp_h
			vel=vel_l
			wnow=wnow_l
	
		#return lum,rad,rp,vel
		lum_diff=in_lum-10.**lum
		rad_diff=in_rad-10.**rad
		vel_diff=in_vel-vel
		
		gof=np.sqrt(lum_diff**2.+rad_diff**2.+vel_diff**2.)
		#print gof,in_lum,in_rad,in_vel,lum_diff,rad_diff,vel_diff
		return 1./gof
	except:
		#print 'Error. Returning high gof'
		return 1./1e5











def read_mesa(masses,omegas,mesa_dir,mesa_use_Z):
	"""Creates mesa_dict - the dictionary of mass tracks stored for the given masses/omegas combo
	
	Inputs:
	masses
		An array of strings representing the masses available for the given mesa_use_Z
	omegas
		An array of strings representing the omegas available for the given mesa_use_Z
	mesa_dir
		The directory in which the mesa output files are stored
	mesa_use_Z
		The internal metallicity to be used
	
	Outputs:
	mesa_dict
		The dictionary of mass tracks stored for the given masses/omegas combo	
	"""
	for i in range(len(masses)):
		for j in range(len(omegas)):
			inp_file=mesa_dir+mesa_use_Z+'_M'+masses[i]+'_w'+omegas[j]
			if i == 0 and j == 0:
				mesa_dict={inp_file:store_mesa(inp_file)}
			else:
				mesa_dict[inp_file]=store_mesa(inp_file)
	return mesa_dict

def store_mesa(fil):
	"""Collects the mass track from a given file and gets the relevant info from it and stores it as a list ready
		to be packaged into mesa_dict
	Inputs:
	fil
		The input file name
		
	Outputs:	The following are in the returned list
	age
		An array with the ages (in Gyr) of the mass track
	teff
		An array with the log(T_eff/K) of the mass track
	lum
		An array with the log(L/L_sun) of the mass track
	rad
		An array with the log(R/R_sun) of the mass track
	r_p
		An array with the log(R_p/R_sun) of the mass track
	vel
		An array with the Equatorial Velocity in km/s of the mass track
	wnow
		An array with the Angular rotation rate/critical at the current age of the mass track
	"""
	age=[]
	teff=[]
	lum=[]
	rad=[]
	r_p=[]
	vel=[]
	wnow=[]
	with open(fil,'r') as input:
		input_reader=csv.reader(input,delimiter=' ',skipinitialspace=True)
		input_reader.next()
		for line in input_reader:
			if len(line)==17 and line[1] != '2' and line[1] !='star_age':
				if float(line[13])*10**(float(line[5])) > 0.0:
					age.append(float(line[1])*1e-9)			#Age in Gyr	
					teff.append(float(line[3]))				#log(T_eff/K)
					lum.append(float(line[4]))				#log(L/L_sun)
					rad.append(float(line[5]))				#log(R/R_sun)
					r_p.append(np.log10(float(line[13])*10**(float(line[5]))))	#log(R_p/R_sun)
					vel.append(float(line[14]))				#Equatorial Velocity in km/s
					wnow.append(float(line[15]))				#Angular rotation rate/critical at the current age

	age=np.array(age)
	teff=np.array(teff)
	lum=np.array(lum)
	rad=np.array(rad)
	r_p=np.array(r_p)
	vel=np.array(vel)
	wnow=np.array(wnow)
	return [age,teff,lum,rad,r_p,vel,wnow]


def read_this_phoenix(this_file,phx_dict,phx_dir,phx_mu,phx_wav,mode,wl_list):
	"""Reads the phoenix model spectra and sets up phx_dict dictionary.
	
	Inputs:
	this_file
		The phoenix .fits file that is being read.
	phx_dict
		A dictionary with all the saved phoenix spectra in it
	phx_dir
		The directory in which the phoenix model spectra are stored
		(this should already have the metallicity taken into account)
	mode
		Single letter string stating whether visibilities ('v'), photometry ('p'),
		or both ('b') are to be calculated.
	wl_list
		List of lists stating what wavelengths are to be used.
		If mode is 'p', wl_list will contain use_filts (the names of the filters
		used by the photometry) and filt_dict (the dictionary that contains
		the filter response curves). 
		If mode is 'v', wl_list will contain uni_wl (the unique wavelengths 
		associated with the observed visibilities) and uni_dwl (the 
		uncertainties associated with uni_wl).
		If mode is 'b', wl_list will contain use_filts, filt_dict, uni_wl, and uni_dwl 
		(in that order).		
	
	Outputs:
	this_file_entry
		The entry this file will have in the phx_dict
	"""
	#print 'Grabbing file {} from {}'.format(tlo_str+glo_str,'this Computer')
	print 'Grabbing this file from the Computer: {}'.format(this_file)
	this_hdulist = pyfits.open(phx_dir+this_file)
	this_arr=this_hdulist[0].data
	this_hdulist.close()
	
	use_filts=wl_list[0]
	filt_dict=wl_list[1]
	uni_wl=wl_list[2]
	uni_dwl=wl_list[3]
		
	
	phot_filtered=[]
	if 'p' in mode:
		for i in range(len(use_filts)):
			inted=[]
			for j in np.arange(len(phx_mu)-1)+1:
				#non_inted is the filtered (i.e., had a filter response curve applied to it), non-integrated version of the intensity array 
				not_inted=this_arr[j-1,:]*filt_dict[use_filts[i]]/fwhm(phx_wav,filt_dict[use_filts[i]])/1e8
				#inted is non_inted that has been integrated over wavelength
				#inted.append(np.trapz(not_inted,x=phx_wav))
				not_inted_reduced = not_inted[np.where(filt_dict[use_filts[i]] > 0)]
				#inted is non_inted_reduced that has been integrated over wavelength
				inted.append(do_phx_integrate(not_inted_reduced))
			phot_filtered.append(inted)
	
	vis_filtered=[]
	if 'v' in mode:
		for i in range(len(uni_wl)):
			inted=[]
			the_filter=np.zeros(len(phx_wav))
			for k in range(len(phx_wav)):
				#This creates a top-hat function for the filter response curve of the visibility observations
				if phx_wav[k]/100. > uni_wl[i]-uni_dwl[i]/2. and phx_wav[k]/100. <= uni_wl[i]+uni_dwl[i]/2.:
					the_filter[k]=1.
			for j in np.arange(len(phx_mu)-1)+1:
				#non_inted is the filtered (i.e., had a filter response curve applied to it), non-integrated version of the intensity array 
				not_inted=this_arr[j-1,:]*the_filter/uni_dwl[i]
				#inted is non_inted that has been integrated over wavelength
				#inted.append(np.trapz(not_inted,x=phx_wav))
				not_inted_reduced = not_inted[np.where(not_inted > 0)]
				#inted is non_inted_reduced that has been integrated over wavelength
				inted.append(do_phx_integrate(not_inted_reduced))
			vis_filtered.append(inted)
	phx_dict[this_file] = [this_arr,phot_filtered,vis_filtered]
	return [this_arr,phot_filtered,vis_filtered]
def read_this_phoenix_ftp(this_file,phx_dict,ftp_dir,phx_mu,phx_wav,mode,wl_list):
	"""Reads the phoenix model spectra and sets up phx_dict dictionary.
	
	Inputs:
	this_file
		The phoenix .fits file that is being read.
	phx_dict
		A dictionary with all the saved phoenix spectra in it
	ftp_dir
		The directory in which the phoenix model spectra are stored
		(this should already have the metallicity taken into account)
	mode
		Single letter string stating whether visibilities ('v'), photometry ('p'),
		or both ('b') are to be calculated.
	wl_list
		List of lists stating what wavelengths are to be used.
		If mode is 'p', wl_list will contain use_filts (the names of the filters
		used by the photometry) and filt_dict (the dictionary that contains
		the filter response curves). 
		If mode is 'v', wl_list will contain uni_wl (the unique wavelengths 
		associated with the observed visibilities) and uni_dwl (the 
		uncertainties associated with uni_wl).
		If mode is 'b', wl_list will contain use_filts, filt_dict, uni_wl, and uni_dwl 
		(in that order).		
	
	Outputs:
	this_file_entry
		The entry this file will have in the phx_dict
	"""
	print '---Grabbing this file from the Internet: {}'.format(this_file)
	this_hdulist = pyfits.open(ftp_dir+this_file)
	this_arr=this_hdulist[0].data
	this_hdulist.close()
	
	use_filts=wl_list[0]
	filt_dict=wl_list[1]
	uni_wl=wl_list[2]
	uni_dwl=wl_list[3]
		
	
	phot_filtered=[]
	if 'p' in mode:
		for i in range(len(use_filts)):
			inted=[]
			for j in np.arange(len(phx_mu)-1)+1:
				#non_inted is the filtered (i.e., had a filter response curve applied to it), non-integrated version of the intensity array 
				not_inted=this_arr[j-1,:]*filt_dict[use_filts[i]]/fwhm(phx_wav,filt_dict[use_filts[i]])/1e8
				#inted is non_inted that has been integrated over wavelength
				#inted.append(np.trapz(not_inted,x=phx_wav))
				not_inted_reduced = not_inted[np.where(filt_dict[use_filts[i]] > 0)]
				#inted is non_inted_reduced that has been integrated over wavelength
				inted.append(do_phx_integrate(not_inted_reduced))
			phot_filtered.append(inted)
	
	vis_filtered=[]
	if 'v' in mode:
		for i in range(len(uni_wl)):
			inted=[]
			the_filter=np.zeros(len(phx_wav))
			for k in range(len(phx_wav)):
				#This creates a top-hat function for the filter response curve of the visibility observations
				if phx_wav[k]/100. > uni_wl[i]-uni_dwl[i]/2. and phx_wav[k]/100. <= uni_wl[i]+uni_dwl[i]/2.:
					the_filter[k]=1.
			for j in np.arange(len(phx_mu)-1)+1:
				#non_inted is the filtered (i.e., had a filter response curve applied to it), non-integrated version of the intensity array 
				not_inted=this_arr[j-1,:]*the_filter/uni_dwl[i]
				#inted is non_inted that has been integrated over wavelength
				#inted.append(np.trapz(not_inted,x=phx_wav))
				not_inted_reduced = not_inted[np.where(not_inted > 0)]
				#inted is non_inted_reduced that has been integrated over wavelength
				inted.append(do_phx_integrate(not_inted_reduced))
			vis_filtered.append(inted)
	phx_dict[this_file] = [this_arr,phot_filtered,vis_filtered]
	return [this_arr,phot_filtered,vis_filtered]

def amoeba(var,scale,func,ftolerance=1.e-4,xtolerance=1.e-4,itmax=500,data=None):
    '''Use the simplex method to maximize a function of 1 or more variables.
    
       Input:
              var = the initial guess, a list with one element for each variable
              scale = the search scale for each variable, a list with one
                      element for each variable.
              func = the function to maximize.
              
       Optional Input:
              ftolerance = convergence criterion on the function values (default = 1.e-4)
              xtolerance = convergence criterion on the variable values (default = 1.e-4)
              itmax = maximum number of iterations allowed (default = 500).
              data = data to be passed to func (default = None).
              
       Output:
              (varbest,funcvalue,iterations)
              varbest = a list of the variables at the maximum.
              funcvalue = the function value at the maximum.
              iterations = the number of iterations used.

       - Setting itmax to zero disables the itmax check and the routine will run
         until convergence, even if it takes forever.
       - Setting ftolerance or xtolerance to 0.0 turns that convergence criterion
         off.  But do not set both ftolerance and xtolerance to zero or the routine
         will exit immediately without finding the maximum.
       - To check for convergence, check if (iterations < itmax).
              
       The function should be defined like func(var,data) where
       data is optional data to pass to the function.

       Example:
       
           import amoeba
           def afunc(var,data=None): return 1.0-var[0]*var[0]-var[1]*var[1]
           print amoeba.amoeba([0.25,0.25],[0.5,0.5],afunc)

       Version 1.0 2005-March-28 T. Metcalf
               1.1 2005-March-29 T. Metcalf - Use scale in simsize calculation.
                                            - Use func convergence *and* x convergence
                                              rather than func convergence *or* x
                                              convergence.
               1.2 2005-April-03 T. Metcalf - When contracting, contract the whole
                                              simplex.
       '''

    nvar = len(var)       # number of variables in the minimization
    nsimplex = nvar + 1   # number of vertices in the simplex
    
    # first set up the simplex

    simplex = [0]*(nvar+1)  # set the initial simplex
    simplex[0] = var[:]
    for i in range(nvar):
        simplex[i+1] = var[:]
        simplex[i+1][i] += scale[i]

    fvalue = []
    for i in range(nsimplex):  # set the function values for the simplex
        fvalue.append(func(simplex[i],data=data))

    # Ooze the simplex to the maximum

    iteration = 0
    
    while 1:
        # find the index of the best and worst vertices in the simplex
        ssworst = 0
        ssbest  = 0
        for i in range(nsimplex):
            if fvalue[i] > fvalue[ssbest]:
                ssbest = i
            if fvalue[i] < fvalue[ssworst]:
                ssworst = i
                
        # get the average of the nsimplex-1 best vertices in the simplex
        pavg = [0.0]*nvar
        for i in range(nsimplex):
            if i != ssworst:
                for j in range(nvar): pavg[j] += simplex[i][j]
        for j in range(nvar): pavg[j] = pavg[j]/nvar # nvar is nsimplex-1
        simscale = 0.0
        for i in range(nvar):
            simscale += abs(pavg[i]-simplex[ssworst][i])/scale[i]
        simscale = simscale/nvar

        # find the range of the function values
        fscale = (abs(fvalue[ssbest])+abs(fvalue[ssworst]))/2.0
        if fscale != 0.0:
            frange = abs(fvalue[ssbest]-fvalue[ssworst])/fscale
        else:
            frange = 0.0  # all the fvalues are zero in this case
            
        # have we converged?
        if (((ftolerance <= 0.0 or frange < ftolerance) and    # converged to maximum
             (xtolerance <= 0.0 or simscale < xtolerance)) or  # simplex contracted enough
            (itmax and iteration >= itmax)):             # ran out of iterations
            return simplex[ssbest],fvalue[ssbest],iteration

        # reflect the worst vertex
        pnew = [0.0]*nvar
        for i in range(nvar):
            pnew[i] = 2.0*pavg[i] - simplex[ssworst][i]
        fnew = func(pnew,data=data)
        if fnew <= fvalue[ssworst]:
            # the new vertex is worse than the worst so shrink
            # the simplex.
            for i in range(nsimplex):
                if i != ssbest and i != ssworst:
                    for j in range(nvar):
                        simplex[i][j] = 0.5*simplex[ssbest][j] + 0.5*simplex[i][j]
                    fvalue[i] = func(simplex[i],data=data)
            for j in range(nvar):
                pnew[j] = 0.5*simplex[ssbest][j] + 0.5*simplex[ssworst][j]
            fnew = func(pnew,data=data)
        elif fnew >= fvalue[ssbest]:
            # the new vertex is better than the best so expand
            # the simplex.
            pnew2 = [0.0]*nvar
            for i in range(nvar):
                pnew2[i] = 3.0*pavg[i] - 2.0*simplex[ssworst][i]
            fnew2 = func(pnew2,data=data)
            if fnew2 > fnew:
                # accept the new vertex in the simplex
                pnew = pnew2
                fnew = fnew2
        # replace the worst vertex with the new vertex
        for i in range(nvar):
            simplex[ssworst][i] = pnew[i]
        fvalue[ssworst] = fnew
        iteration += 1
        #if __debug__: print ssbest,fvalue[ssbest]
def calc_vis(r,R_p,beta,dist,lomg,OMG,m,vis,vis_err,wl,u_l,v_l,u_m,v_m,uni_wl,g_scale,perim_x,perim_y,n_params,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list,cal,y_above,x_above,star,model,model_dir):
	"""Calculates the visibility
	Inputs:
	vis
		The measured visibilities
	vis_err
		The uncertainties in the measured visibilities
	uni_wl
		The unique wavelengths associated with the observed visibilities
	g_scale
		The scale required such that ~1000 of the pixels in the model image are of the star
	perim_x
		x coordinates that make up the perimeter of the star
	perim_y
		y coordinates that make up the perimeter of the star
	n_params
		The number of parameters being altered
		
	Outputs:
	vis_chi2
		The chi^2 value comparing the observed and modeled visibilities.
	"""
	R_e,V_e,inc,T_p,pa=r
	#res=4900 #This sets the size of the model image (i.e. the image is an array of size res+1 x res+1)
	res=4095
	#res=4899
	
	mod_vis=np.zeros(len(vis))
	bl=np.arange(res+1.) #Baseline in meters
	bl*=g_scale
	vis_time=0.
	for index in range(len(uni_wl)):
		image_start=time.time()
		intensity_array=np.zeros((res+1,res+1),dtype=np.complex64) #This will be where the model image gets stored
		dlin=unitrange(res)*uni_wl[index]-uni_wl[index]/2. #This sets the scale of the image
		dlin/=g_scale
		
		#This next bit goes through the model image, line by line (for each quadrant of the star) and first checks
			#if the point is inside the star's perimeter (defined earlier). If it is, it will calculate the flux at that 
			#point with the extract function. If it's not, it will move to the next line. 
		g_points=0
		#Top Half
		yi=res/2
		yj=True
		while yj:
			#print yi
			#Right Side
			xi=res/2
			xj=True
			ilist=[]
			while xj:
				if inside.inside(dlin[xi],dlin[yi],perim_x,perim_y):
					try:
						iii=extract(dlin[xi],dlin[yi],inc,R_e,T_p,beta,R_p,dist,lomg,OMG,m,pa,index,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list)
					except:
						print 'An error occured in extract(1). Returning with high chi^2.'
						return 1e8,0
					intensity_array[yi,xi]=iii
					ilist.append(iii)
					xi+=1
					g_points+=1
				else:
					xj=False
			#Left Side
			xi=res/2-1
			xj=True
			ilist=[]
			while xj:
				if inside.inside(dlin[xi],dlin[yi],perim_x,perim_y):
					try:
						iii=extract(dlin[xi],dlin[yi],inc,R_e,T_p,beta,R_p,dist,lomg,OMG,m,pa,index,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list)
					except:
						print 'An error occured in extract(2). Returning with high chi^2.'
						return 1e8,0
					intensity_array[yi,xi]=iii
					ilist.append(iii)
					xi-=1
					g_points+=1
				else:
					xj=False
			if ilist == []:
				yj=False
			else:
				yi+=1
		#Bottom Half
		yi=res/2-1
		yj=True
		while yj:
			#print yi
			#Right Side
			xi=res/2
			xj=True
			ilist=[]
			while xj:
				if inside.inside(dlin[xi],dlin[yi],perim_x,perim_y):
					try:
						iii=extract(dlin[xi],dlin[yi],inc,R_e,T_p,beta,R_p,dist,lomg,OMG,m,pa,index,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list)
					except:
						print 'An error occured in extract(3). Returning with high chi^2.'
						return 1e8,0
					intensity_array[yi,xi]=iii
					ilist.append(iii)
					xi+=1
					g_points+=1
				else:
					xj=False
			#Left Side
			xi=res/2-1
			xj=True
			ilist=[]
			while xj:
				if inside.inside(dlin[xi],dlin[yi],perim_x,perim_y):
					try:
						iii=extract(dlin[xi],dlin[yi],inc,R_e,T_p,beta,R_p,dist,lomg,OMG,m,pa,index,phx_dir,phx_dict,use_Z,tg_lists,phx_mu,phx_wav,mode,wl_list)
					except:
						print 'An error occured in extract(4). Returning with high chi^2.'
						return 1e8,0
					intensity_array[yi,xi]=iii
					ilist.append(iii)
					xi-=1
					g_points+=1
				else:
					xj=False
			if ilist == []:
				yj=False
			else:
				yi-=1
		
		intensity_array/=np.amax(intensity_array) #Normalize the intensity array
		image_finish=time.time()
		image_time=image_finish-image_start
		#print 'Image generation took {} seconds'.format(image_time)
		#print 'Scale: {}, Number of points: {}'.format(g_scale,g_points)
		fft_start=time.time()	#To calculate how long the fft takes to compute
		#=====================#
		#    FFT DONE HERE    #
		#=====================#
		if 'g' not in mode:
			v=np.fft.fft2(intensity_array)	#Does the actual transform
		if 'g' in mode:
			cuda.init()
			context=make_default_context()
			stream=cuda.Stream()
			plan=Plan((res+1,res+1),stream=stream)
			gpu_data=gpuarray.to_gpu(intensity_array)
			plan.execute(gpu_data)
			v=gpu_data.get()
			context.pop()
		
		fft_finish=time.time()	#To calculate how long the fft takes to compute
		fft_time=fft_finish-fft_start	#To calculate how long the fft takes to compute
		#print 'FFT took {} seconds'.format(fft_time)
		v=abs(v)	#Only use real component
		v/=np.amax(v)	#Normalize
		
		
		#imgplot=plt.imshow(intensity_array,cmap=cm.gray)
		#plt.show()
		#imgplot=plt.imshow(v,cmap=cm.gray)
		#plt.show()
		
		
		sf=bl/uni_wl[index]-max(bl)/uni_wl[index]/2.	#The spatial frequency vector
		sf_ind=np.arange(len(sf))	#The indicies of the spatial frequency vector
		#This for loop extracts the visibility at the measured spatial frequencies (u_l and v_l)
		#vis_start=time.time()
		for i in range(len(vis)):
			if wl[i] == uni_wl[index]:
				#Bilinear interpolation time
				geu=sf[np.where(sf >= u_l[i])]
				leu=sf[np.where(sf <= u_l[i])]
				ulo=max(leu)
				uhi=min(geu)
				gev=sf[np.where(sf >= v_l[i])]
				lev=sf[np.where(sf <= v_l[i])]
				vlo=max(lev)
				vhi=min(gev)
				
				ui=sf_ind[np.where(sf == ulo)]
				vi=sf_ind[np.where(sf == vlo)]
				if u_l[i] < 0:
					ui+=res/2
				else:
					ui-=res/2
				if v_l[i] < 0:
					vi+=res/2
				else:
					vi-=res/2
				ll=v[ui,vi]
				ui=sf_ind[np.where(sf == uhi)]
				vi=sf_ind[np.where(sf == vlo)]
				if u_l[i] < 0:
					ui+=res/2
				else:
					ui-=res/2
				if v_l[i] < 0:
					vi+=res/2
				else:
					vi-=res/2
				hl=v[ui,vi]
				ui=sf_ind[np.where(sf == ulo)]
				vi=sf_ind[np.where(sf == vhi)]
				if u_l[i] < 0:
					ui+=res/2
				else:
					ui-=res/2
				if v_l[i] < 0:
					vi+=res/2
				else:
					vi-=res/2
				lh=v[ui,vi]
				ui=sf_ind[np.where(sf == uhi)]
				vi=sf_ind[np.where(sf == vhi)]
				if u_l[i] < 0:
					ui+=res/2
				else:
					ui-=res/2
				if v_l[i] < 0:
					vi+=res/2
				else:
					vi-=res/2
				hh=v[ui,vi]
				int_v=ll/(uhi-ulo)/(vhi-vlo)*(uhi-u_l[i])*(vhi-v_l[i])+hl/(uhi-ulo)/(vhi-vlo)*(u_l[i]-ulo)*(vhi-v_l[i])+lh/(uhi-ulo)/(vhi-vlo)*(uhi-u_l[i])*(v_l[i]-vlo)+hh/(uhi-ulo)/(vhi-vlo)*(u_l[i]-ulo)*(v_l[i]-vlo)
				mod_vis[i]=int_v[0]
				#vis_finish=time.time()
				#vis_time+=vis_finish-vis_start
	#print 'Visibilities took {} seconds'.format(vis_time)
	diff_vis=vis-mod_vis	#Observed minus modeled
	bol=np.sqrt(u_l**2.+v_l**2.)	#B/lambda, the 1D spatial frequency
	
	#plt.plot(bol,vis,'ro')
	#plt.plot(bol,mod_vis,'bs')
	#plt.show()
	
	if 'P' in mode:
		plot_ellipse(vis,vis_err,bol,u_m,v_m,cal,y_above,x_above,star,model,model_dir)
		plot_vis(vis,vis_err,mod_vis,diff_vis,bol,star,model,model_dir)
	
	vis_chi2=sum(diff_vis**2./vis_err**2.)/(float(len(diff_vis))-n_params-1.)	#The chi^2 based on the visibilities
	return vis_chi2,g_points
def calc_phot(r,R,tht_R,T_eff,g,g_r,g_t,lg,OMG,phot_data,colat,phi,sin_colat,cos_colat,cos_phi,sin_inc,cos_inc,filt_dict,use_filts,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,zpf,cwl,mode,wl_list,n_params,star,model,model_dir):
	"""Calculates the photometry
	Inputs:
	
	Outputs:
	phot_chi2
		The chi^2 value comparing the observed and modeled photometry.
	"""
	R_e,V_e,inc,T_p,pa=r
	#Calculating Photometry
	phot_phi=dict()
	for f in filt_dict:
		phot_phi[f]=[]
	for j in range(len(phi)):
		phot_col=dict()
		for f in filt_dict:
			phot_col[f]=[]
		mu=1.0/g*(-1.0*g_r*(sin_colat*sin_inc*cos_phi[j]+cos_colat*cos_inc)-g_t*(sin_inc*cos_phi[j]*cos_colat-sin_colat*cos_inc))
		for i in range(len(colat)):
			for f in filt_dict:
				if mu[i] < 0.034962:
					phot_col[f].append(0.)
				else:
					phot_col[f].append(extract_phoenix_phot(T_eff[i],lg[i],mu[i],f,use_filts,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,mode,wl_list)*(tht_R[i])**2.*mu[i]*np.sin(colat[i]))
					
		for f in filt_dict:
			phot_phi[f].append(np.trapz(phot_col[f],x=colat))
	filt_fluxes=dict()
	phot_dict=dict()
	phot_diff=[]
	phot_err=[]
	for f in filt_dict:
		filt_fluxes[f]=np.trapz(phot_phi[f],x=phi)
		phot_dict[f]=-2.5*np.log10(filt_fluxes[f]/zpf[f])
		phot_diff.append(filt_fluxes[f]-zpf[f]*10.**(-0.4*float(phot_data[f][0])))
		phot_err.append(float(phot_data[f][1])*zpf[f]*0.4*np.log(10.)*10.**(-0.4*float(phot_data[f][0])))
	phot_diff=np.array(phot_diff)
	phot_err=np.array(phot_err)

	if 'P' in mode:
		plot_phot(R,tht_R,T_eff,g,g_r,g_t,lg,OMG,V_e,inc,phx_wav,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phot_data,colat,phi,mode,wl_list,filt_dict,filt_fluxes,zpf,cwl,star,model,model_dir)
		
	phot_chi2=sum(phot_diff**2./phot_err**2.)/(float(len(phot_diff))-n_params-1.)
	
	return phot_chi2
def calc_Lbol(r,R,tht_R,T_eff,g,g_r,g_t,lg,dist,phx_mu,colat,phi,sin_colat,cos_colat,cos_phi,sin_inc,cos_inc,phx_dir,use_Z,tg_lists,phx_dict,phx_wav,mode,wl_list):
	"""Calculates the bolometric and apparent luminosities
	Inputs:
	
	Outputs:
	L_bol
		The bolometric luminosity
	L_app
		The apparent luminosity
	"""
	R_e,V_e,inc,T_p,pa=r	

	#The wavelength range of the phoenix model I use is limited, so beyond that range (in both directions), I assume blackbody
	lo_wav=np.arange(400)+100.	#Wavengths < phx_wav (in A)
	lo_wav/=1e8	#convert lo_wav from A to cm
	hi_wav=np.arange(5000)*10.+26000.	#Wavelengths > phx_wav (in A)
	hi_wav/=1e8	#convert hi_wav from A to cm
	
	integrand=[]
	lo_integrand=[]
	hi_integrand=[]
	#This for loop defines the array over which to integrate to get the total luminosity. The functional form looks like this: L_bol=2 pi int(from x=0 to pi) (I_bol*R^2*sin(x)) dx where x is the colatitude
	for i in range(len(colat)):
		I_lam_mu=[]
		for l in range(len(phx_mu)):
			II=extract_phoenix_full(T_eff[i],lg[i],phx_mu[l],phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,mode,wl_list)
			I_lam_mu.append(phx_mu[l]*II)
		I_lam_mu=np.array(I_lam_mu)
		I_lam=np.zeros(len(phx_wav))
		for l in range(len(phx_wav)):
			if l == 0:
				I_lam[l]=0.
			else:
				I_lam[l]=np.trapz(I_lam_mu[:,l],x=phx_mu)
		lo_I_lam=2.*h*c**2./(lo_wav)**5.*1./(np.exp(h*c/k/T_eff[i]/(lo_wav))-1)	#Blackbody intensity spectrum for wavelength < phx_wav
		hi_I_lam=2.*h*c**2./(hi_wav)**5.*1./(np.exp(h*c/k/T_eff[i]/(hi_wav))-1)	#Blackbody intensity spectrum for wavelength > phx_wav
		I_bol=np.trapz(I_lam,x=phx_wav)*2.*np.pi	#Integrate over wavelength
		lo_I_bol=np.trapz(lo_I_lam,x=lo_wav)*2.*np.pi
		hi_I_bol=np.trapz(hi_I_lam,x=hi_wav)*2.*np.pi
		integrand.append(I_bol*(R[i]*R_sun)**2.*sin_colat[i])	#Add the results to the array
		lo_integrand.append(lo_I_bol*(R[i]*R_sun)**2.*sin_colat[i])
		hi_integrand.append(hi_I_bol*(R[i]*R_sun)**2.*sin_colat[i])
	#Integrate over the colatitude (the 2pi is the integration over the longitude)
	L_lo=2.*np.pi*np.trapz(lo_integrand,x=colat)/L_sun
	L_mid=2.*np.pi*np.trapz(integrand,x=colat)/L_sun
	L_hi=2.*np.pi*np.trapz(hi_integrand,x=colat)/L_sun
	L_bol=L_lo+L_mid+L_hi
	if 'o' in mode:
		print 'L_bol: ',L_bol,' L_sun' 
	#Calculating L_app
	i_phi=[]
	lo_i_phi=[]
	hi_i_phi=[]
	for j in range(len(phi)):
		phot_col=dict()
		i_col=[]
		lo_i_col=[]
		hi_i_col=[]
		mu=1.0/g*(-1.0*g_r*(sin_colat*sin_inc*cos_phi[j]+cos_colat*cos_inc)-g_t*(sin_inc*cos_phi[j]*cos_colat-sin_colat*cos_inc))
		for i in range(len(colat)):
			if mu[i] < 0.034962:
				i_col.append(0.)
				lo_i_col.append(0.)
				hi_i_col.append(0.)
			else:
				I_lam=extract_phoenix_full(T_eff[i],lg[i],mu[i],phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,mode,wl_list)*(tht_R[i])**2.*mu[i]*sin_colat[i]
				i_col.append(np.trapz(I_lam,x=phx_wav))
				lo_bb=2.*h*c**2./(lo_wav)**5.*1./(np.exp(h*c/k/T_eff[i]/(lo_wav))-1)*(tht_R[i])**2.*mu[i]*sin_colat[i]*2.
				lo_i_col.append(np.trapz(lo_bb,x=lo_wav))
				hi_bb=2.*h*c**2./(hi_wav)**5.*1./(np.exp(h*c/k/T_eff[i]/(hi_wav))-1)*(tht_R[i])**2.*mu[i]*sin_colat[i]*2.
				hi_i_col.append(np.trapz(hi_bb,x=hi_wav))
		i_phi.append(np.trapz(i_col,x=colat))
		lo_i_phi.append(np.trapz(lo_i_col,x=colat))
		hi_i_phi.append(np.trapz(hi_i_col,x=colat))

	lo_F=np.trapz(lo_i_phi,x=phi)
	mid_F=np.trapz(i_phi,x=phi)
	hi_F=np.trapz(hi_i_phi,x=phi)
	
	F_app=lo_F+mid_F+hi_F
	
	L_app=4.*np.pi*F_app*(dist*pc)**2./L_sun
	if 'o' in mode:
		print 'L_app: ',L_app,' L_sun' 
	
	return L_bol,L_app
def read_vis(vis_inp):
	wl=[]		#Wavelength of observation
	wlerr=[]		#Width of the bandpass used
	vis=[]		#Measured visibility
	vis_err=[]		#Error in the measured visibility
	u_m=[]		#u-coordinate in meters
	v_m=[]		#v-coordinate in meters
	u_l=[]		#u-coordinate in lambdas
	v_l=[]		#v-coordinate in lambdas
	cal=[]		#Calibrator
	with open(vis_inp,'r') as input:
		input_reader=csv.reader(input,delimiter=' ',skipinitialspace=True)
		input_reader.next()
		i=-1
		for line in input_reader:
			if line[0][0] !='#':
				i+=1
				if len(line)==9:
					wl.append(float(line[0]))
					wlerr.append(float(line[1]))
					vis.append(float(line[2]))
					vis_err.append(float(line[3]))
					u_m.append(float(line[4]))
					v_m.append(float(line[5]))
					u_l.append(float(line[6]))
					v_l.append(float(line[7]))
					cal.append(line[8])
				else:
					print "There's an error in your *.vis file (line {}). Please make sure it's in the proper format!".format(i)
	#Turning all the lists into arrays so we can do math with them
	wl=np.array(wl)
	wlerr=np.array(wlerr)
	vis=np.array(vis)
	vis_err=np.array(vis_err)
	u_m=np.array(u_m)
	v_m=np.array(v_m)
	u_l=np.array(u_l)
	v_l=np.array(v_l)
	cal=np.array(cal)
	the_sort=np.argsort(wl)
	vis=vis[the_sort]
	vis_err=vis_err[the_sort]
	u_m=u_m[the_sort]
	v_m=v_m[the_sort]
	u_l=u_l[the_sort]
	v_l=v_l[the_sort]
	cal=cal[the_sort]
	wlerr=wlerr[the_sort]
	wl=wl[the_sort]
	#Convert wavelengths from um to m
	wlerr*=1e-6
	wl*=1e-6
	return wl,wlerr,vis,vis_err,u_m,v_m,u_l,v_l,cal
def read_phot(phot_inp):
	phot_data=dict()
	use_filts=[]
	with open(phot_inp,'r') as input:
		input_reader=csv.reader(input,delimiter='\t')
		input_reader.next()
		for line in input_reader:
			if line[0][0] != '#':
				phot_data[line[0]]=[line[1],line[2]]
				use_filts.append(line[0])
	return phot_data,use_filts
def read_cwlzpf(cwlzpf_file):
	data=ascii.read(cwlzpf_file)
	filt=list(data['waveband'])
	cwl=list(data['central_wavelength_in_cm'])
	zpf=list(data['zero_point_flux_in_erg/s/cm^2/cm'])
	cwl_dict=dict()
	zpf_dict=dict()
	for i in range(len(filt)):
		cwl_dict[filt[i]]=float(cwl[i])
		zpf_dict[filt[i]]=float(zpf[i])
	return cwl_dict,zpf_dict
def get_phoenix_wave(phx_dir):
	"""Reads the phoenix model spectra and sets up phx_dict dictionary.
	
	Inputs:
	phx_dir
		The directory in which the phoenix model spectra are stored
		(this should already have the metallicity taken into account)
	
	Outputs:
	phx_wav
		An array of wavelength values used by the model phoenix spectra
	"""
	print 'Using the Phoenix atmospere models'
	phx_file='Z-0.0/lte07000-4.00-0.0.PHOENIX-ACES-AGSS-COND-SPECINT-2011.fits' #An example file to get the dictionary started
	hdulist = pyfits.open(phx_dir+phx_file) #Read phx_file
	unfiltered = hdulist[0].data #The intensity array
	hdulist.close()
	
	unfiltered=list(unfiltered)
	unfiltered=np.array(unfiltered)
	phx_wav=(np.arange(len(unfiltered[-1]))+500.)*1e-8 #This defines the wavelength array
	
	return phx_wav

def get_complex_trf(arr):
	print 'get_complex_trf has been called'
    complex_dtype = dtypes.complex_for(arr.dtype)
    return Transformation(
        [Parameter('output', Annotation(Type(complex_dtype, arr.shape), 'o')),
        Parameter('input', Annotation(arr, 'i'))],
        """
        ${output.store_same}(
            COMPLEX_CTR(${output.ctype})(
                ${input.load_same},
                0));
        """)

def do_phx_integrate(y):
	z=np.trapz(y,dx=1.e-8)
	return z
def read_input(input_file):
	import re
	
	if input_file == '':
		from Tkinter import Tk
		from tkFileDialog import askopenfilename
		Tk().withdraw() # we don't want a full GUI, so keep the root window from appearing
		filename = askopenfilename() # show an "Open" dialog box and return the path to the selected file
	else:
		filename = input_file
	data_dict=dict()
	with open(filename,'r') as input:
		input_reader=csv.reader(input,delimiter='|',skipinitialspace=True)
		input_reader.next()
		for line in input_reader:
			for i in range(len(line)):
				line[i] = re.sub('\t', '', line[i])
			data_dict[line[0]]=line[1]
			
	star=data_dict['Star']
	model=data_dict['Model']
	star_dir=data_dict['Star Directory']+star+'/'
	model_dir=star_dir+model+'/'
	confirm_file=model_dir+star+'.confirm'
	open(confirm_file,'a').write('Time is {}'.format(time.ctime(time.time())))
	open(confirm_file,'w').write('\n Input file {} has been read and is running. \n The following flags have been set:'.format(filename))
	if data_dict['Calc Vis'] == 'Y': open(confirm_file,'a').write('\n Visibilities will be calculated')
	if data_dict['Calc Phot'] == 'Y': open(confirm_file,'a').write('\n Photometry will be calculated')
	if data_dict['Calc Lum'] == 'Y': open(confirm_file,'a').write('\n Luminosity will be calculated')
	if data_dict['Calc Age'] == 'Y': open(confirm_file,'a').write('\n Age will be calculated')
	if data_dict['GPU Accel'] == 'Y': open(confirm_file,'a').write('\n GPU acceleration will be used')
	if data_dict['Verbose'] == 'Y': open(confirm_file,'a').write('\n Verbose mode will be used')
	if data_dict['Gravity Darkening'] == 'vZ': open(confirm_file,'a').write('\n The vZ gravity darkening law will be used')
	if data_dict['Gravity Darkening'] == 'ELR': open(confirm_file,'a').write('\n The ELR gravity darkening law will be used')

	return data_dict
	
def plot_ellipse(vis,vis_err,bol,u_m,v_m,cal,y_above,x_above,star,model,model_dir):
	from matplotlib import pyplot as plt
	import matplotlib.cm as cm
	import pylab as pyl
	
	show_data=True
	#Here is the part where I fit uniform disk diameters to each of the visibilities and plot the
	#    resulting angular radius
	x_ell1=[]
	y_ell1=[]
	x_ell_hiv1=[]
	y_ell_hiv1=[]
	x_ell_lov1=[]
	y_ell_lov1=[]
	x_ell2=[]
	y_ell2=[]
	x_ell_hiv2=[]
	y_ell_hiv2=[]
	x_ell_lov2=[]
	y_ell_lov2=[]
	
	thtr=(0.0001+np.arange(100000)*0.0001)/206264806.
	for i in np.arange(len(vis)):
		x=2.*thtr*np.pi*bol[i]
		v=abs(2.*jn(1.,x)/x)
		pos=min(enumerate(v), key=lambda z: abs(z[1]-vis[i]))
		ar=x[pos[0]]/2./np.pi/bol[i]
		psi=np.arctan(u_m[i]/v_m[i])+np.pi/2.
		x_ell1.append(ar*np.cos(psi))
		y_ell1.append(ar*np.sin(psi))
		x_ell2.append(ar*np.cos(psi+np.pi))
		y_ell2.append(ar*np.sin(psi+np.pi))
		pos=min(enumerate(v), key=lambda z: abs(z[1]-(vis[i]+vis_err[i])))
		ar=x[pos[0]]/2./np.pi/bol[i]
		x_ell_hiv1.append(ar*np.cos(psi))
		y_ell_hiv1.append(ar*np.sin(psi))
		x_ell_hiv2.append(ar*np.cos(psi+np.pi))
		y_ell_hiv2.append(ar*np.sin(psi+np.pi))
		pos=min(enumerate(v), key=lambda z: abs(z[1]-(vis[i]-vis_err[i])))
		ar=x[pos[0]]/2./np.pi/bol[i]
		x_ell_lov1.append(ar*np.cos(psi))
		y_ell_lov1.append(ar*np.sin(psi))
		x_ell_lov2.append(ar*np.cos(psi+np.pi))
		y_ell_lov2.append(ar*np.sin(psi+np.pi))
	x_ell1=np.array(x_ell1)*206264806.
	y_ell1=np.array(y_ell1)*206264806.
	x_ell2=np.array(x_ell2)*206264806.
	y_ell2=np.array(y_ell2)*206264806.
	x_ell_hiv1=np.array(x_ell_hiv1)*206264806.
	y_ell_hiv1=np.array(y_ell_hiv1)*206264806.
	x_ell_lov1=np.array(x_ell_lov1)*206264806.
	y_ell_lov1=np.array(y_ell_lov1)*206264806.
	x_ell_hiv2=np.array(x_ell_hiv2)*206264806.
	y_ell_hiv2=np.array(y_ell_hiv2)*206264806.
	x_ell_lov2=np.array(x_ell_lov2)*206264806.
	y_ell_lov2=np.array(y_ell_lov2)*206264806.
	
	unical=[]
	for i in range(len(cal)):
		if cal[i] not in unical:
			unical.append(cal[i])
	colors=np.array(['bo','ro','ko','go','yo','mo','co','kd'])
	color_errs=np.array(['b-','r-','k-','g-','y-','m-','c-','k-'])
	color_names=np.array(['blue','red','black','green','yellow','magenta','cyan','black diamond'])
	
	#pyl.plot([-0.0289998180123],[-0.13147874836],'ro')
	#pyl.plot([0.309598004788],[-0.053898186997],'bo')
	
	y_dots=np.array(y_above)*206264806.
	x_dots=np.array(x_above)*206264806.
	#pyl.rcParams.update({'font.size': 15})
	pyl.plot(y_dots,-x_dots,'k+',markersize=2)
	if show_data:
		for i in range(len(vis)):
			index=-1
			for j in range(len(unical)):
				if cal[i] == unical[j]:
					index=j
			pyl.plot([x_ell_hiv1[i],x_ell_lov1[i]],[y_ell_hiv1[i],y_ell_lov1[i]],color_errs[0],linewidth=3)
			pyl.plot([x_ell_hiv2[i],x_ell_lov2[i]],[y_ell_hiv2[i],y_ell_lov2[i]],color_errs[0],linewidth=3)
			pyl.plot(np.array(x_ell1[i]),np.array(y_ell1[i]),colors[0],markersize=10)
			pyl.plot(np.array(x_ell2[i]),np.array(y_ell2[i]),colors[0],markersize=10)
		#for j in range(len(unical)):
		#	print unical[j],color_names[j]
	pyl.xlabel('East - West (mas)',{'color':'k','fontsize':15})
	pyl.ylabel('South - North (mas)',{'color':'k','fontsize':15})
	pyl.axis('equal')
	pyl.axis([-max([max(x_ell1),max(y_ell1),max(y_dots),max(x_dots)])*1.1,max([max(x_ell1),max(y_ell1),max(y_dots),max(x_dots)])*1.1,-max([max(x_ell1),max(y_ell1),max(y_dots),max(x_dots)])*1.1,max([max(x_ell1),max(y_ell1),max(y_dots),max(x_dots)])*1.1])
	#pyl.axis([-max([max(x_above),max(y_above)])*1.1,max([max(x_above),max(y_above)])*1.1,-max([max(x_above),max(y_above)])*1.1,max([max(x_above),max(y_above)])*1.1])
	pyl.savefig(model_dir+star+'_'+model+'_ellplot.pdf')
	print 'Model plotted. {}'.format(star+'_'+model+'_ellplot.pdf')

def plot_vis(vis,vis_err,mod_vis,diff_vis,bol,star,model,model_dir):
	from matplotlib import pyplot as plt
	import matplotlib.cm as cm
	import pylab as pyl
	
	squared=True
	
	if squared:
		vis=vis**2.
		mod_vis=mod_vis**2.
		diff_vis=vis-mod_vis
	plt.rcParams.update({'font.size': 15})
	ax1 = plt.subplot2grid((4, 1), (0, 0),rowspan=3)
	ax2 = plt.subplot2grid((4, 1), (3, 0))
	for i in np.arange(len(vis)):
		ax1.plot([bol[i],bol[i]],[vis[i],mod_vis[i]],'r--')
		ax1.plot([bol[i],bol[i]],[vis[i]+vis_err[i],vis[i]-vis_err[i]],'k-')
	ax1.plot(bol,vis,'ro',markersize=16)
	ax1.plot(bol,mod_vis,'bs',markersize=8)
	ax1.set_xlim(np.amin(bol)-1e7,np.amax(bol)+1e7)
	ax1.set_ylim(0.0,1.0)
	if squared:
		ax1.set_ylabel('Squared Visibility')
	else:
		ax1.set_ylabel('Visibility')
	for i in np.arange(len(vis)):
		ax2.plot([bol[i],bol[i]],[vis[i]+vis_err[i]-mod_vis[i],vis[i]-vis_err[i]-mod_vis[i]],'k-')
	ax2.plot([0,1e10],[0,0],'k--')
	ax2.plot(bol,diff_vis,'ro')
	ax2.set_xlim(np.amin(bol)-1e7,np.amax(bol)+1e7)
	ax2.set_ylabel('O - C')
	plt.xlabel('Spatial frequency (rad'+r'$^{-1}$)')
	plt.subplots_adjust(hspace=0)
	plt.setp(ax1.get_xticklabels(), visible=False)
	plt.savefig(model_dir+star+'_'+model+'_visanddiff.pdf')
	plt.close()
	print 'Visibilities and their residuals plotted. {}'.format(star+'_'+model+'_visanddiff.pdf')

def plot_phot(R,tht_R,T_eff,g,g_r,g_t,lg,OMG,V_e,inc,phx_wav,phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phot_data,colat,phi,mode,wl_list,filt_dict,filt_fluxes,zpf,cwl,star,model,model_dir):
	from matplotlib import pyplot as plt
	import matplotlib.cm as cm
	import pylab as pyl

	full_spec=[]
	phx_flux_dict=dict()
	for kk in range(len(phx_wav)):
		phx_flux_dict[phx_wav[kk]]=0.
	phd_phi=dict()
	for kk in phx_flux_dict:
		phd_phi[kk]=[]
	#print 'Mid: Step 1/3'
	for j in range(len(phi)):
		phd_col=dict()
		for kk in phx_flux_dict:
			phd_col[kk]=[]
		mu=1.0/g*(-1.0*g_r*(np.sin(colat)*np.sin(inc)*np.cos(phi[j])+np.cos(colat)*np.cos(inc))-g_t*(np.sin(inc)*np.cos(phi[j])*np.cos(colat)-np.sin(colat)*np.cos(inc)))
		for i in range(len(colat)):
			if mu[i] < 0.034962:
				for kk in phx_flux_dict:
					phd_col[kk].append(0.)
			else:
				I_lam=extract_phoenix_full(T_eff[i],lg[i],mu[i],phx_dir,use_Z,tg_lists,phx_mu,phx_dict,phx_wav,mode,wl_list)*(tht_R[i])**2.*mu[i]*np.sin(colat[i])
				if 'd' in mode:
					#print 'Colat: {} || Phi: {}'.format(180./np.pi*colat[i],180./np.pi*phi[j])
					#Tangential Velocity - km/s
					V_t=OMG*R[i]*R_sun/1e5*np.sin(colat[i])
					#Inclined Tangential Velocity - km/s
					V_it=V_t*np.sin(inc)
					#Line of Sight Velocity - km/s
					V_los=V_it*np.cos(phi[j]+np.pi)
					#print 'Colatitude: {} deg || Longitude: {} deg || Inclination: {} deg || R[i]: {} cm || V_eq: {} km/s || V_t: {} km/s || V_it: {} km/s || V_los: {} km/s'.format(180./np.pi*colat[i],180./np.pi*phi[j],180./np.pi*inc,R[i]*R_sun,V_e,V_t,V_it,V_los)
					redshift=V_los*1e5/c
					this_wav=phx_wav*(1.+redshift)
					this_wav=list(this_wav)
					I_lam=list(I_lam)
					diff=this_wav[1]-this_wav[0]
					while min(this_wav) > min(phx_wav):
						new_wav_0=this_wav[0]-diff
						this_wav.insert(0,new_wav_0)
						I_lam.insert(0,0.)
					while max(this_wav) < max(phx_wav):	
						new_wav_end=this_wav[-1]+diff
						this_wav.append(new_wav_end)
						I_lam.append(0.)
					this_wav=np.array(this_wav)
					I_lam=np.array(I_lam)
					f=interp1d(this_wav,I_lam)
					#print min(this_wav),max(this_wav)
					#print min(phx_wav),max(phx_wav)
					#print max(this_wav)-max(phx_wav)
					I_lam=f(phx_wav)
				for kk in range(len(phx_wav)):
					phd_col[phx_wav[kk]].append(I_lam[kk])
		for kk in phx_flux_dict:
			phd_phi[kk].append(np.trapz(phd_col[kk],x=colat))
		#i_phi.append(np.trapz(i_col,x=colat))
	#print 'Mid: Step 2/3'
	for kk in phx_flux_dict:
		phx_flux_dict[kk]=np.trapz(phd_phi[kk],x=phi)
	#print 'Mid: Step 3/3'
	for kk in range(len(phx_wav)):
		full_spec.append(phx_flux_dict[phx_wav[kk]]*1e-8)
	
	full_spec=np.array(full_spec)
	
	if 'd' in mode:
		plt.plot(phx_wav*1e8,full_spec*1e8*phx_wav,linestyle='-',label='Inc: {} deg'.format(inc*180./np.pi))
		if 'f' in mode:
			#Zoomin
			#xmin=1.275e-4
			#xmax=1.29e-4
			#ymin=5e-8
			#ymax=2e-7
			##Zoomout
			xmin=1e-5
			xmax=2.6e-4
			ymin=1e-9
			ymax=1e-6
			plt.legend(loc=4)
			plt.xlim(xmin*1e8,xmax*1e8)
			plt.xscale('log')
			plt.yscale('log')
			sed_inrange=full_spec[np.where(phx_wav >= xmin)]
			new_wave=phx_wav[np.where(phx_wav >= xmin)]
			sed_inrange*=new_wave*1e8
			sed_inrange=sed_inrange[np.where(new_wave <= xmax)]
			#ymin=np.amin(sed_inrange)*0.9
			#ymax=np.amax(sed_inrange)*2.
			plt.ylim(ymin,ymax)
			plt.savefig(model_dir+star+'_'+model+'_photanddiff.pdf')
			plt.close()
			print 'Photometry and their residuals plotted. {}'.format(star+'_'+model+'_photanddiff.pdf')	
	else:
		plt.rcParams.update({'font.size': 15})
		ax1 = plt.subplot2grid((4, 1), (0, 0),rowspan=3)
		ax2 = plt.subplot2grid((4, 1), (3, 0))
		ax1.plot(phx_wav,full_spec,color='0.4',linestyle='-')
		for f in filt_dict:
			this_flux=zpf[f]*10.**(-0.4*float(phot_data[f][0]))
			this_flux_err=float(phot_data[f][1])*zpf[f]*0.4*np.log(10.)*10.**(-0.4*float(phot_data[f][0]))
			if this_flux > this_flux_err:
				ax1.plot([cwl[f],cwl[f]],[this_flux+this_flux_err,this_flux-this_flux_err],'k-')
			else:
				ax1.plot([cwl[f],cwl[f]],[this_flux+this_flux_err,1e-20],'k-')
			ax1.plot(cwl[f], this_flux,'ro',markersize=16)
			#print cwl[f],this_flux,filt_fluxes[f]
			ax1.plot(cwl[f], filt_fluxes[f],'bs',markersize=8)
			#print 'Band: {}, Measured phot: {}, Modeled phot: {}'.format(f,phot_data[f][0],-2.5*log10(filt_fluxes[f]/zpf[f]))
		plt.xlabel('Wavelength (cm)')
		ax1.set_xscale('log')
		ax1.set_ylabel('Flux (erg/s/cm^2/A)')
		ax1.set_yscale('log')
		ax1.set_xlim(1e-5,2.6e-4)
		sed_inrange=full_spec[(phx_wav >= 1e-5).nonzero()]
		new_wave=phx_wav[(phx_wav >= 1e-5).nonzero()]
		sed_inrange=sed_inrange[(new_wave <= 2.6e-4).nonzero()]
		ax1.set_ylim(np.amin(sed_inrange)*0.9,np.amax(sed_inrange)*1.1)
		
		ax2.plot([1e-10,1e10],[0,0],'k--')
		for f in filt_dict:
			this_flux=zpf[f]*10.**(-0.4*float(phot_data[f][0]))
			this_flux_err=float(phot_data[f][1])*zpf[f]*0.4*np.log(10.)*10.**(-0.4*float(phot_data[f][0]))
			ax2.plot([cwl[f],cwl[f]],[(this_flux+this_flux_err-filt_fluxes[f])/filt_fluxes[f]*100.,(this_flux-this_flux_err-filt_fluxes[f])/filt_fluxes[f]*100.],'k-')
			ax2.plot(cwl[f], (this_flux-filt_fluxes[f])/filt_fluxes[f]*100.,'ro')
		ax2.set_xlabel('Wavelength (cm)')
		ax2.set_xscale('log')
		ax2.set_ylabel('Percent Difference')
		ax2.set_xlim(1e-5,2.6e-4)
		plt.setp(ax1.get_xticklabels(), visible=False)
		plt.subplots_adjust(hspace=0)
		plt.savefig(model_dir+star+'_'+model+'_photanddiff.pdf')
		plt.close()
		print 'Photometry and their residuals plotted. {}'.format(star+'_'+model+'_photanddiff.pdf')
	

#
