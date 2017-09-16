'''
Molecular Density Matrix Embedding theory
ref: 
J. Chem. Theory Comput. 2016, 12, 2706−2719
PHYSICAL REVIEW B 89, 035140 (2014)
Author: Hung Q. Pham, Unviversity of Minnesota
email: phamx494@umn.edu
'''

import sys
import numpy as np
from scipy import optimize
from functools import reduce
from . import orthobasis, smithbasis, qcsolvers
sys.path.append('./lib/build')
import libdmet

class DMET:
	def __init__(self, mf, impCluster, symmetry, orthogonalize_method = 'overlap', smith_decomposition_method = 'OED', OEH_type = 'FOCK', SC_CFtype = 'FB', solver = 'RHF'):
		'''
		Args:
			mf 							: a rhf wave function from pyscf
			impCluster					: A list of arrays, each array is an embedding system with fragment orbitals labeled by 1,
										  environment orbitals (not bath) labeled by 0.
			symmetry					: either 'Translation' or a list of symmetry labels, fragments are symmetrically equivalent if they have the same label
			orthogonalize_method 		: overlap/boys/lowdin/meta_lowdin
			smith_decomposition_method	: OED/overlap
			OEH_type					: One-electron Hamiltonian used in the bath construction, h = OEH + umat 
			embedding_symmetry			: a list of integer numbers indicating how the fragments are relevant by symmetry,
										  defaut: non-symmetry 
			embedding_solvers			: a list of solvers for each fragment
										  defaut: use the same solver for all fragments	
			SCmethod					: CG/SLSQP/BFGS/L-BFGS-B/LSTSQ self-consistent iteration method, defaut: BFGS
			SC_threshold				: convergence criteria for correlation potential, default: 1e-6
			SC_maxcycle                 : maximum cycle for self-consistent iteration, default: 50
			SC_CFtype					: FB/diagFB/F/diagF, cost function type, fitting 1RDM of the entire Smith basis (FB), diagonal FB (diagFB), 
										  fragment (F), or diagonal elements of fragment only (diagF), default: FB
			umat						: correlation potential
			chempot						: global chemical potential
			emb_1RDM					: a list of the 1RDM for each fragment
			emb_orbs					: a list of the fragment and bath orbitals for each fragment			
		Return:
		
		'''		
		
		# General initialized attributes 
		self.mf = mf
		self.Norbs = mf.mol.nao_nr()
		self.Nelecs = mf.mol.nelectron
		self.numPairs = self.Nelecs//2		
		self.impCluster = impCluster
		self.num_impCluster = len(impCluster)		
		self.imp_size = self.make_imp_size()
		
		self.orthobasis = orthobasis.Orthobasis(mf, orthogonalize_method)
		self.sd_type = smith_decomposition_method
		self.OEH_type = OEH_type
		self.single_embedding = False

		# Symmetry		
		if symmetry == None:
			self.symmetry = list(range(self.num_impCluster))
		elif symmetry == 'Translation':
			assert (self.Norbs/self.imp_size[0]).is_integer()	#Check if Translational symmetry can be used
			self.num_impCluster = 1
			self.symmetry = [0]
		else:
			assert isinstance(symmetry, list)
			assert len(symmetry) == self.num_impCluster
			self.symmetry = symmetry
	
		self.irred_fragments, self.inverse_indices = np.unique(self.symmetry, return_inverse=True)
		self.irred_size = self.irred_fragments.size

		# QC Solver	
		solver_list = ['RHF', 'CASCI', 'CASSCF', 'DMRG', 'CCSD']
		if isinstance(solver, list):
			assert len(solver) == self.num_impCluster
			self.solver = solver
			for fragment in self.irred_fragments:
				assert self.solver[fragment] in solver_list		
		else:
			assert solver in solver_list
			self.solver = [solver]*self.num_impCluster
		self.CAS = [None]*self.num_impCluster	# (n,m) means n electron in m orbitals
		self.CAS_MO = [None]*self.num_impCluster

		# Self-consistent parameters	
		self.SC_method = 'BFGS'
		self.SC_threshold = 1e-5
		self.SC_maxcycle =	200	
		self.SC_CFtype = SC_CFtype
		self.SC_damping = 0.0

		# Correlation/chemical potential
		self.mask, self.redundant = self.make_mask()
		self.H1start, self.H1row, self.H1col = self.make_H1()[1:4]	#Use in the calculation of 1RDM derivative 
		self.uvec = self.make_uvec()
		self.Nterms = self.uvec.size		
		self.chempot = 0.0

		# DMET Output		
		self.emb_1RDM = []	
		self.emb_orbs = []		
		self.fragment_energies = []
		self.fragment_nelecs = []
		self.Energy_total = None
		
		# Others
		np.set_printoptions(precision=6)
		
	def kernel(self, chempot = 0.0, single_embedding = False):
		'''
		This is the main kernel for DMET calculation.
		It is solving the embedding problem for each fragment, then returning the total number of electrons 
		and updating the smith orbitals and 1RDM for each fragment
		Args:
			chempot					: global chemical potential to adjust the numer of electrons in each fragment
		Return:
			fragment_nelecs.sum() 	: the total number of electrons
		Update the class attributes:
			fragment_energies		: an array of the energy for each fragment.   
			fragment_nelecs			: an array of the number of electrons for each fragment		
			emb_1RDM				: an array of the 1RDM for each fragment				
		'''			
		self.fragment_energies = []
		self.fragment_nelecs = []
		self.emb_1RDM = []
		self.emb_orbs = []
		
		orthoOED = self.orthobasis.construct_orthoOED(self.uvec2umat(self.uvec), self.OEH_type)		# get both MO coefficients and 1-RDM in orthonormal basis
		
		for fragment in self.irred_fragments:
			impOrbs = np.abs(self.impCluster[fragment])
			numImpOrbs  = np.sum(impOrbs)
			numBathOrbs = numImpOrbs
			smith = smithbasis.RHF_decomposition(self.mf, impOrbs, numBathOrbs, orthoOED)
			smith.method = self.sd_type		
			numBathOrbs, FBEorbs, envOrbs_or_core_eigenvals = smith.baths()

			
			Norb_in_imp  = numImpOrbs + numBathOrbs
			assert(Norb_in_imp <= self.Norbs)
			
			if self.sd_type == 'OED' :
				core_cutoff = 0.01
				for cnt in range(len(envOrbs_or_core_eigenvals)):
					if (envOrbs_or_core_eigenvals[cnt] < core_cutoff):
						envOrbs_or_core_eigenvals[cnt] = 0.0
					elif (envOrbs_or_core_eigenvals[cnt] > 2.0 - core_cutoff):
						envOrbs_or_core_eigenvals[cnt] = 2.0
					else:
						print ("Bad DMET bath orbital selection: trying to put a bath orbital with occupation", envOrbs_or_core_eigenvals[cnt], "into the environment :-(.")
						assert(0 == 1)	
				Nelec_in_imp = int(round(self.Nelecs - np.sum(envOrbs_or_core_eigenvals)))
				Nelec_in_environment = np.sum(np.abs(envOrbs_or_core_eigenvals))				
				core1RDM_ortho = reduce(np.dot, (FBEorbs, np.diag(envOrbs_or_core_eigenvals), FBEorbs.T))				
			elif self.sd_type == 'overlap':
				Nelec_in_imp = int(2*numImpOrbs)
				Nelec_in_environment = self.Nelecs - Nelec_in_imp
				core1RDM_ortho = 2*np.dot(FBEorbs[:,Norb_in_imp:], FBEorbs[:,Norb_in_imp:].T)				
				
			#Transform the 1e/2e integrals and the JK core constribution to Smith basis
			dmetOEI  = self.orthobasis.dmet_oei(FBEorbs, Norb_in_imp)
			dmetTEI  = self.orthobasis.dmet_tei(FBEorbs, Norb_in_imp)			
			dmetCoreJK = self.orthobasis.dmet_corejk(FBEorbs, Norb_in_imp, core1RDM_ortho)
			
			#Solving the embedding problem with high level wfs
			solver = self.solver[fragment]
			print("    Solving the irreducible fragment %2d [%2d eletrons in (%2d fragment + %2d bath )] by %s solver" % (fragment, Nelec_in_imp, numImpOrbs, numBathOrbs, solver))						
			DMguess = reduce(np.dot,(FBEorbs[:,:Norb_in_imp].T, orthoOED[1], FBEorbs[:,:Norb_in_imp]))
			qcsolver = qcsolvers.QCsolvers(dmetOEI, dmetTEI, dmetCoreJK, DMguess, Norb_in_imp, Nelec_in_imp, numImpOrbs, chempot)
			if solver == 'RHF':
				ImpEnergy, E_emb, RDM1 = qcsolver.RHF()
			elif solver == 'UHF':
				pass
			elif solver == 'CASCI':
				ImpEnergy, E_emb, RDM1 = qcsolver.CAS(self.CAS[fragment], self.CAS_MO[fragment], Orbital_optimization = False)
			elif solver == 'CASSCF':
				ImpEnergy, E_emb, RDM1 = qcsolver.CAS(self.CAS[fragment], self.CAS_MO[fragment], Orbital_optimization = True)			
			elif solver == 'DMRG':
				pass
			elif solver == 'CCSD':
				pass			
				
			#Collecting the energies/RDM1/no of electrons for each fragment
			#if single_embedding == True, then self.fragment_energies is a list of the embedding energy, core1RDM, Nelec_in_environment (not rounded)
			if single_embedding == False:
				self.fragment_energies.append(ImpEnergy)				
			else:
				self.fragment_energies.extend([E_emb, core1RDM_ortho, Nelec_in_environment])
				
			self.emb_1RDM.append(RDM1)
			self.emb_orbs.append(FBEorbs[:,:Norb_in_imp])
			ImpNelecs = np.trace(RDM1[:numImpOrbs,:numImpOrbs])
			self.fragment_nelecs.append(ImpNelecs)
		
		#Transform the irreducible energy/electron lists to the corresponding full lists
		if single_embedding == False:
			self.fragment_energies = np.asarray(self.fragment_energies)[self.inverse_indices]
		self.fragment_nelecs = np.asarray(self.fragment_nelecs)[self.inverse_indices]	
		
		multiplicty = 1.0
		if self.symmetry == [0]: multiplicty = self.imp_size.size		
		return self.fragment_nelecs.sum()*multiplicty

	def one_shot(self):
		'''
		Do one-shot DMET, only the chemical potential is optimized
		'''
		print("-- ONE-SHOT DMET CALCULATION : START --")
		
		if self.single_embedding == True:
			assert len(self.impCluster) == 1		
			Fragment_nelecs = self.kernel(chempot = 0.0, single_embedding = True)
			E_embedding = self.fragment_energies[0]
			orthoOED_core = self.fragment_energies[1]
			Jcore = np.einsum('pqrs,rs->pq', self.orthobasis.orthoTEI, orthoOED_core)
			Kcore = np.einsum('prqs,rs->pq', self.orthobasis.orthoTEI, orthoOED_core)
			JKcore = Jcore - 0.5*Kcore
			E_core = 0.5*(orthoOED_core*(2*self.orthobasis.orthoOEI + JKcore)).sum()
			print('-----Single-embedding energy decoposition-----')			
			print('Embedding energy              : ' , E_embedding, ' a.u.') 
			print('Pure/Core environment energy  : ' , E_core, ' a.u.') 
			Energy_total = E_embedding + E_core + self.mf.energy_nuc()
			self.fragment_nelecs = np.asarray(self.emb_1RDM[0]).trace() + self.fragment_energies[2]		
		else:
			self.chempot = optimize.newton(self.nelecs_costfunction, self.chempot, tol = 1.e-10)
			multiplicty = 1
			if self.symmetry == [0]: multiplicty = self.imp_size.size
			Energy_total = self.fragment_energies.sum()*multiplicty + self.mf.energy_nuc()
		
		self.Energy_total = Energy_total
		print("Fragment energies: ", self.fragment_energies)
		print("Fragment electrons: ", self.fragment_nelecs)	
		print("Total energy: ", Energy_total)			
		print("-- ONE-SHOT DMET CALCULATION : END --")
		print()			
	def self_consistent(self):
		'''
		Do self-consistent DMET
		'''	
		print("- SELF-CONSISTENT DMET CALCULATION : START -")
		
		u_diff = 1.0
		umat = np.zeros((self.Norbs, self.Norbs))
		
		for cycle in range(self.SC_maxcycle):
			
			print("DMET cycle : ", cycle + 1)
			umat_old = umat
			
			# Do one-shot with each uvec
			self.one_shot()
			print ("Chemical potential = ", self.chempot)

			# Optimize uvec
			if self.SC_method == 'BFGS':
				result = optimize.minimize(self.costfunction, self.uvec, method='BFGS', jac=self.costfunction_gradient, options={'disp': False})
			elif self.SC_method == 'CG':
				result = optimize.minimize(self.costfunction, self.uvec, method='CG', jac = self.costfunction_gradient, options={'disp': False})
			else:
				print(self.SC_method, " is not supported")
			umat = self.uvec2umat(result.x)
			umat = umat - np.eye(umat.shape[0])*np.average(np.diag(umat))
			u_diff = np.linalg.norm(umat_old - umat)
			umat = self.SC_damping*umat_old + (1.0 - self.SC_damping)*umat
			print("2-norm of difference old and new u-mat: ", u_diff ) 
			if u_diff <= self.SC_threshold: break
			
		print("--- SELF-CONSISTENT DMET CALCULATION : END ---")
		
	def nelecs_costfunction(self, chempot):
		'''
		The different in the correct number of electrons (provided) and the calculated one 
		'''
		
		Nelec_dmet = self.kernel(chempot)
		Nelec_target = self.Nelecs			
		print ("   Chemical potential , number of electrons = " , chempot, "," , Nelec_dmet ,"")

		return Nelec_dmet - Nelec_target	

	def costfunction(self, uvec):
		'''
		Cost function: CF(u) = Sum_x (Sum_rs (corrD_x_rs(u) - mfD_x_rs(u))^2) = Sum_x (Sum_rs (rdm_diff_x_rs(u))^2)
		'''
		frags_error = []
		rdm_diff = self.rdm_diff(uvec)
		for fragment in range(self.irred_size):
			error = np.power(rdm_diff[fragment], 2).sum()
			frags_error.append(error)
		frags_error = np.asarray(frags_error)[self.inverse_indices]		#Transform irreducible array to the full array
		return frags_error.sum()
		
	def costfunction_gradient(self, uvec):
		'''
		Analytical derivative of the cost function,
		deriv(CF(u)) = Sum_x [Sum_rs (2 * rdm_diff_x_rs(u) * deriv(rdm_diff_x_rs(u))]
		ref: ref: J. Chem. Theory Comput. 2016, 12, 2706−2719
		'''
		
		the_rdm_diff = self.rdm_diff(uvec)
		the_rdm_diff_gradient = self.rdm_diff_gradient(uvec)
		CF_gradient = np.zeros(self.Nterms)
		
		for u in range(self.Nterms):
			frag_gradient = []
			for fragment in range(self.irred_size):
				gradient = np.sum(2 * the_rdm_diff[fragment] * the_rdm_diff_gradient[u][fragment])
				frag_gradient.append(gradient)
			frag_gradient = np.asarray(frag_gradient)[self.inverse_indices]		#Transform irreducible array to the full array
			CF_gradient[u] = frag_gradient.sum()
		
		return CF_gradient
		
		
	def rdm_diff(self, uvec):
		'''
		Calculating the different between mf-1RDM (transformed in Smith basis) and correlated-1RDM for each
		embedding problem (each fragment), or rdm_diff_x_rs(u) in self.costfunction()
		Args:
			uvec		: the correlation potential vector
		Return:
			the_rdm_diff	: a list with the size of the number of irreducible fragment, each element is a numpy array of 
						  errors for each fragment.
		'''
		
		orthoOED = self.orthobasis.construct_orthoOED(self.uvec2umat(uvec), self.OEH_type)[1]
		the_rdm_diff = []
		
		for fragment in range(self.irred_size):
			transform_mat = self.emb_orbs[fragment]			#Smith basis transformation matrix
			if self.SC_CFtype == 'FB' or self.SC_CFtype == 'diagFB':
				mf_1RDM = reduce(np.dot, (transform_mat.T, orthoOED, transform_mat))
				corr_1RDM = self.emb_1RDM[fragment]				
				if self.SC_CFtype == 'FB': error = mf_1RDM - corr_1RDM
				if self.SC_CFtype == 'diagFB': error = np.diag(mf_1RDM) - np.diag(corr_1RDM)	
				
			elif self.SC_CFtype == 'F' or self.SC_CFtype == 'diagF':
				mf_1RDM = reduce(np.dot, (transform_mat[:,:self.imp_size[fragment]].T, orthoOED, transform_mat[:,:self.imp_size[fragment]]))
				corr_1RDM = self.emb_1RDM[fragment][:self.imp_size[fragment], :self.imp_size[fragment]]			
				if self.SC_CFtype == 'F': error = mf_1RDM - corr_1RDM
				if self.SC_CFtype == 'diagF': error = np.diag(mf_1RDM) - np.diag(corr_1RDM)	
			the_rdm_diff.append(error)
		return the_rdm_diff

	def rdm_diff_gradient(self, uvec):
		'''
		Compute the rdm_diff gradient
		Args:
			uvec			: the correlation potential vector
		Return:
			the_gradient	: a list with the size of the number of u values in uvec, each element is a list with the size of the number
							 of irreducible fragment. Each element of this list is a numpy array of derivative corresponding to each rs.
							 
		'''
		
		RDM_deriv = self.construct_1RDM_response(uvec)
		
		the_gradient = []
		for u in range(self.Nterms):
			frag_gradient = []
			for fragment in range(self.irred_size):
				transform_mat = self.emb_orbs[fragment]			#Smith basis transformation matrix
				if self.SC_CFtype == 'FB' or self.SC_CFtype == 'diagFB':
					error_deriv_smith = reduce(np.dot, (transform_mat.T, RDM_deriv[u,:,:], transform_mat))				
					if self.SC_CFtype == 'diagFB': error_deriv_smith = np.diag(error_deriv_smith)	
					
				elif self.SC_CFtype == 'F' or self.SC_CFtype == 'diagF':
					error_deriv_smith = reduce(np.dot, (transform_mat[:,:self.imp_size[fragment]].T, RDM_deriv[u,:,:], transform_mat[:,:self.imp_size[fragment]]))	
					if self.SC_CFtype == 'diagF': error_deriv_smith = np.diag(error_deriv_smith)
				frag_gradient.append(error_deriv_smith)
			the_gradient.append(frag_gradient)
		return the_gradient

######################################## USEFUL FUNCTION for DMET class ######################################## 
		
	def make_imp_size(self):
		'''
		Make an array of the numbers of fragment/impurity orbitals for each fragment
		Modified from QC-DMET, Copyright (C) 2015 Sebastian Wouters
		'''
		imp_sizes = []
		for fragment in range(self.num_impCluster):
			impurityOrbs = np.abs(self.impCluster[fragment])
			numImpOrbs = np.sum(impurityOrbs)
			imp_sizes.append(numImpOrbs)
		imp_sizes = np.array(imp_sizes)
		return imp_sizes

	def make_uvec(self):
		'''
		Create the chemical potential vector (uvec) with regard to the symmetry and the cost function type
		'''
		umat = np.zeros((self.Norbs, self.Norbs))
		uvec = umat[self.mask]	
		return uvec

	def uvec2umat(self, uvec):
		'''
		Convert uvec to the umat which is will be added up to the one-electron Hamiltonian
		'''	
		umat = np.zeros((self.Norbs, self.Norbs))
		umat[self.mask] = uvec
		umat = umat.T
		umat[self.mask] = uvec
		
		if self.symmetry == [0]:			#Translational symemtry is used
			size = self.imp_size[0]
			for it in range( 1, len(self.impCluster)):
				umat[it*size:(it+1)*size, it*size:(it+1)*size] = umat[0:size,0:size]
		elif self.irred_size < self.num_impCluster:
			for block in self.redundant:
				start1, start2, size = block
				umat[start1:(start1 + size), start1:(start1 + size)] = umat[start2:(start2 + size), start2:(start2 + size)]
				
		return umat

	def make_mask(self):
		'''
		Create a Norbs x Norbs matrix with 'True' at the location of fragment orbitals, symmetry is considered
		'''	
		themask = np.zeros([self.Norbs, self.Norbs], dtype=bool)
	
		irred_fragments = self.irred_fragments.tolist()
		redundant = []	
		for fragment in range(self.num_impCluster):
			frag_ID = self.symmetry[fragment]
			if frag_ID in irred_fragments:
				start = self.imp_size[:fragment].sum()
				if self.SC_CFtype == 'diagF' or self.SC_CFtype == 'diagFB': #Only fitting the diagonal elements of umat
					for localsize in range(self.imp_size[fragment]):
						for row in range(localsize + 1):
							themask[start + row, start + row] = True
				else:
					for localsize in range(self.imp_size[fragment]):		#Fitting the whole umat
						for row in range(localsize + 1):
							for col in range(row, localsize + 1):
								themask[start+ row, start + col] = True				
				irred_fragments.remove(frag_ID)
			else:
				start1 = self.imp_size[:fragment].sum()
				start2_id = [frag for frag in range(self.num_impCluster) if self.symmetry[frag] == frag_ID][0]
				start2 = self.imp_size[:start2_id].sum()
				size = self.imp_size[fragment]				
				redundant.append([start1, start2, size])

		return themask, redundant				
		
	def make_H1(self):
		'''
		The H1 is the corelation potential operator, used to calculate gradient of 1-RDM
		Return:
			H1start:
			H1row:
			H1col:
		'''
		theH1 = []
		irred_fragments = self.irred_fragments.tolist()
		for fragment in range(self.num_impCluster):
			frag_ID = self.symmetry[fragment]
			if frag_ID in irred_fragments:
				if self.symmetry == [0]:											#Translational symemtry is used
					start_id = list(range(self.imp_size.size))
				else:
					start_id = [frag for frag in range(self.num_impCluster) if self.symmetry[frag] == frag_ID]
				
				if self.SC_CFtype == 'diagF' or self.SC_CFtype == 'diagFB': 		#Only fitting the diagonal elements of umat
					for row in range(self.imp_size[fragment]):
						H1 = np.zeros([self.Norbs, self.Norbs])
						for id in start_id:
							start = self.imp_size[:id].sum()
							H1[start + row, start + row] = 1
						theH1.append(H1)
				else:		
					for row in range(self.imp_size[fragment]):						#Fitting the whole umat
						for col in range(row, self.imp_size[fragment]):
							H1 = np.zeros([self.Norbs, self.Norbs])
							for id in start_id:
								start = self.imp_size[:id].sum()
								H1[start + row, start + col] = 1
								H1[start + col, start + row] = 1								
							theH1.append(H1)	
				irred_fragments.remove(frag_ID)
	
		#Convert the sparse H1 to one dimension H1start, H1row, H1col arrays used in libdmet.rhf_response()
		H1start = []
		H1row   = []
		H1col   = []
		H1start.append(0)
		totalsize = 0
		for count in range(len(theH1)):
			rowco, colco = np.where(theH1[count] == 1)
			totalsize += len(rowco)
			H1start.append(totalsize )
			for count2 in range(len(rowco)):
				H1row.append(rowco[count2])
				H1col.append(colco[count2])
		H1start = np.array(H1start)
		H1row   = np.array(H1row)
		H1col   = np.array(H1col)	
		return theH1, H1start, H1row, H1col
		
	def construct_1RDM_response(self, uvec):
		'''
		Calculate the derivative of 1RDM
		'''
		orthoFOCK = self.orthobasis.orthoFOCK + self.uvec2umat(uvec)
		rdm_deriv = libdmet.rhf_response(self.Norbs, self.Nterms, self.numPairs, self.H1start, self.H1row, self.H1col, orthoFOCK)
		return rdm_deriv