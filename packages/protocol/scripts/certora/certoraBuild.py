#!/usr/bin/env python3

import json
import os
import sys
from Crypto.Hash import keccak
import subprocess
import shutil
import traceback
import re
import platform
from collections import OrderedDict
from datetime import datetime
import distutils.dir_util
import certoraUtils
from certoraUtils import debug_print, is_windows, safe_create_dir, run_cmd, get_file_basename, get_file_extension, read_from_conf, handle_file_list, get_file_name, current_conf_to_file
from certoraUtils import OPTION_SOLC, OPTION_SOLC_MAP, OPTION_PATH, OPTION_OUTPUT, OPTION_OUTPUT_FOLDER, OPTION_OUTPUT_VERIFY, OPTION_VERIFY, OPTION_LINK, OPTION_PACKAGES, OPTION_PACKAGES_PATH, OPTION_CACHE, DEFAULT_CONF, OPTION_ADDRESS

def print_usage(): # TODO use the macros in print usage as well?
	print("""Usage: 
       If no arguments, read from default.conf
       Otherwise:	   
       [file[:contractName] ...] or CONF_FILE.conf
       [--cache NAME]	
       [--output OUTPUT_FILE_NAME (default: .certora_build)]
       [--output_folder OUTPUT_FOLDER_NAME (default: .certora_config)]	   
       [--link [contractName:slot=contractName ...]]
       [--address [contractName:address ...] (default: auto-assigned)]
       [--path ALLOWED_PATH (default: $PWD/contracts/)]
       [--packages_path PATH (default: $NODE_PATH)] or [--packages [name=path,...]]
       [--solc SOLC_EXEC (default: solc)] or [--solc_map [name=solc,..]]
       [--verify [contractName:specName, ...]]
       [--dont_fetch_sources]
       [--iscygwin]
       [--varmap]
       [--debug]
       [--help]""")

LAST_BUILD_TIME = ".last_build_time"
LAST_VERIFY_GEN_TIME = ".last_verify_gen_time"

try:		
	if "--help" in sys.argv:
		print_usage()
		sys.exit(1)
		
	if "--debug" in sys.argv:
		certoraUtils.DEBUG = True
		
	parsed_options = {"solc":"solc"}
	solc_mappings = {}
	files = []
	fileToContractName = {}
	
	# Remove the python file
	args = sys.argv[1:]
	# Figure out indices where there are options
	enumerated_args = [(i,arg) for i,arg in enumerate(args)]
	debug_print("Enumerated args %s" % (enumerated_args))
	options = list(filter(lambda x: x[1].startswith("--"), enumerated_args))
	debug_print("Options indices %s" % (options))
	if len(options) > 0:
		firstOptionIndex = options[0][0]
		lastFileIndex = firstOptionIndex-1
	else:
		firstOptionIndex = -1
		lastFileIndex = len(args)-1
	debug_print("First option index is %s, last file index is %s" % (firstOptionIndex,lastFileIndex))
	
	if lastFileIndex == -1:
		debug_print("Will read from default.conf")
		read_from_conf(DEFAULT_CONF,parsed_options,files,fileToContractName)
		print("Building verification environment for files: %s" % (files))	
	elif lastFileIndex == 0 and args[lastFileIndex].endswith(".conf"):
		debug_print("Will read from conf file %s" % (args[lastFileIndex]))
		read_from_conf(args[lastFileIndex],parsed_options,files,fileToContractName)
		print("Building verification environment for files: %s" % (files))	
	else:
		# Fetch files
		#if firstOptionIndex == 0:
		#	raise Exception("Error: Must provide a Solidity file to check: number of args is %d and first option index is %d"  % (len(args),firstOptionIndex))
			
		file_list = args[0:lastFileIndex+1]
		handle_file_list(file_list,files,fileToContractName)

		print("Building verification environment for files: %s" % (files))	
		# FROM THIS POINT ONWARD, files & fileToContractName DOES NOT CHANGE

	# Process options - may override those from conf file
	def process_option(option,value):
		debug_print("Processing option %s with value %s" % (option,value))
		option_name = option[1][2:]
		# normalize for non-list options
		if option_name in ["solc","path","packages_path","output","output_folder","solc_map","cache"]:
			assert len(value) == 1
			value = value[0] 
		elif option_name in ["packages"]:
			value = ' '.join(value)
		elif option_name in ["address"]:
			value = dict(map(lambda x: tuple(x.split(":",2)), value))
			
		parsed_options[option_name] = value
		

	for optionIdx,option in [(i,option) for i,option in enumerate(options)]:
		debug_print("Working on option %d %s out of %d" % (optionIdx+1,option,len(options)))
		if optionIdx+1 < len(options):
			nextOption = options[optionIdx+1]
			if nextOption[0] == option[0]+1:
				process_option(option,True)
			else:
				optionParams = args[option[0]+1:nextOption[0]]
				process_option(option,optionParams)
		else:
			if option[0]+1 < len(args):
				value = args[option[0]+1:]
				process_option(option,value)
			else:
				process_option(option,[True])
		
	debug_print("Options: %s" % (parsed_options))	

	# HANDLE OPTION DEFAULTS
	# Add default for "output"
	if OPTION_OUTPUT not in parsed_options:
		parsed_options[OPTION_OUTPUT] = ".certora_build"
		
	# Add default for "output_folder"
	if OPTION_OUTPUT_FOLDER not in parsed_options:
		parsed_options[OPTION_OUTPUT_FOLDER] = ".certora_config"
		
	if OPTION_OUTPUT_VERIFY not in parsed_options:
		parsed_options[OPTION_OUTPUT_VERIFY] = ".certora_verify"

	# Add default for "path"
	if OPTION_PATH not in parsed_options:
		parsed_options[OPTION_PATH] = "%s/contracts/" % (os.getcwd().replace("\\","/"))
		
	# Add default packages path
	if OPTION_PACKAGES_PATH not in parsed_options:
		parsed_options[OPTION_PACKAGES_PATH] = os.getenv("NODE_PATH","%s/node_modules" % (os.getcwd())).replace("\\","/")
		
	# If packages were not specified, try to find them from package.json, if it exists
	if OPTION_PACKAGES not in parsed_options:
		try:
			with open("package.json","r") as package_json_file:
				package_json = json.load(package_json_file)
				deps = set(list(package_json["dependencies"].keys()) if "dependencies" in package_json else list() + list(package_json["devDependencies"].keys()) if "devDependencies" in package_json else list()) # May need both
				# Don't know which ones we need, so we take them all
				#solidity_deps = [k for k in deps.keys() if k.find("solidity") != -1]
				#debug_print("Solidity dependencies: %s" % (solidity_deps))
				
				packages_to_pass_list = [ "%s=%s/%s/" % (package,parsed_options[OPTION_PACKAGES_PATH],package) for package in deps]
				packages_to_pass = ' '.join(packages_to_pass_list)
				debug_print("Packages to pass: %s" % (packages_to_pass))
				parsed_options[OPTION_PACKAGES] = packages_to_pass
		except EnvironmentError:
			debug_print("Failed in processing package.json: %s,%s" % (sys.exc_info()[0:2]))

	# Add default for addresses - empty
	if OPTION_ADDRESS not in parsed_options:
		parsed_options[OPTION_ADDRESS] = {}
		
	if OPTION_SOLC_MAP in parsed_options:
		solcmaps = parsed_options[OPTION_SOLC_MAP]
		split = solcmaps.split(",")
		for solcmap in split:
			contract = solcmap.rsplit("=")[0]
			solcver = solcmap.rsplit("=")[1]
			debug_print("Adding solc mapping from %s to %s" % (contract,solcver))
			solc_mappings[contract]=solcver
			
	
	
	
	### FROM THIS POINT ONWARD, parsed_options and solc_mappings are not changed!
	# Store current options
	current_conf_to_file(parsed_options, files, fileToContractName)
	
	# Start to collect information from solc
	SDCs = {} # SDCs describes the set of all 'Single Deployed Contracts' the solidity file whose contracts comprise a single bytecode of interest. Which one it is - we don't know yet, but we make a guess based on the base filename. An SDC corresponds to a single solidity file. 

	# Note that the the last '/' in config_path is important for solc to succeed, so it should be added
	CERTORA_CONFIG = parsed_options[OPTION_OUTPUT_FOLDER]
	certoraUtils.config_path = "%s/%s" % (os.getcwd().replace("\\","/"), CERTORA_CONFIG)
	config_path = certoraUtils.config_path # A shorter variable to refer to config_path object

	library_addresses = [] # Will become primary contracts too
		
	safe_create_dir(certoraUtils.config_path)	
		
	if "iscygwin" in parsed_options:
		cygwin_config_path = "/%s" % (config_path[0].lower() + config_path[1:].replace(":",""))
		print("Cygwin config path: %s" % (cygwin_config_path))
			

	def collect_funcs(contract,data):	
		funcs = []
		abi = data["contracts"][contract]["abi"]
		for f in filter(lambda x: x["type"]=="function",json.loads(abi)):
			inputs = f["inputs"]
			#inputTypes = ",".join(map(lambda x: "\"%s\""%(x["type"]), inputs))
			inputTypes = [x["type"] for x in inputs]
			if "outputs" in f:
				outputs = f["outputs"]
				#outputTypes = ",".join(map(lambda x: "\"%s\""%(x["type"]), outputs))
				outputTypes = [x["type"] for x in outputs]
			else:
				outputTypes = ""
			if "payable" not in f:
				isNotPayable = False
			else:
				isNotPayable = not f["payable"]	# Only if something is definitely non-payable, we treat it as such
			
			# Nice to have hex too
			base = "%s(%s)" % (f["name"],','.join([x.replace('"','') for x in inputTypes]))
			hash = keccak.new(digest_bits=256)
			hash.update(str.encode(base))
			hex = hash.hexdigest()[0:8]
						
			funcs.append({"name":f["name"],"args":inputTypes,"returns":outputTypes,"sighash":hex,"notpayable":isNotPayable})


		# Add funcs from hashes (because of libraries for instance, that have empty ABI but do have hashes.)
		for funcstr,hash in data["contracts"][contract]["hashes"].items():
			debug_print("Got hash for %s with hash %s" % (funcstr,hash))
			# We assume funcstr hash structure name(arg,..)
			openParenIdx = funcstr.find("(")
			lastParenIdx = funcstr.find(")")
			assert lastParenIdx > openParenIdx and openParenIdx > -1
			name = funcstr[0:openParenIdx]
			argsstr = funcstr[openParenIdx+1:lastParenIdx]
			args = [x for x in argsstr.split(",") if x.strip()]
			# TODO: We won't know output types which is unfortunate... the only way right now is to get it from the AST which is somewhat complicated
			
			if (name,args) in [(x["name"],x["args"]) for x in funcs]: # if function already appeared in ABI:
				prev_func = [x for x in funcs if x["name"]==name and x["args"]==args][0]
				debug_print("Found another instance of %s(%s)" % (name,args))
				# Make sure it has the same signature!
				assert prev_func["sighash"] == hash, "There is already a function names %s, args %s, but hash %s with found %s" % (name,prev_func["args"],prev_func["sighash"],hash)
			else: # Otherwise, add with available information
				print("Found an instance of %s(%s) that did not appear in ABI" % (name,args))
				funcs.append({"name":name,"args":args,"returns":[],"sighash":hash})
			
			
		return funcs
		
	def collect_srclist(data):
		return {i:src for i,src in enumerate(data["sourceList"])}
		
	def collect_srcmap(contract,data):
		return data["contracts"][contract]["srcmap-runtime"]

	def collect_varmap(contract,data):
		return data["contracts"][contract]["local-mappings"]

	def collect_name_file(contract):
		lastIndexOfColon = contract.rfind(':')
		name = contract[lastIndexOfColon+1:]
		file = contract[0:lastIndexOfColon]
		return name,file

	def collect_contracts(data):
		contracts = [c for c in data["contracts"]]
		return contracts
			
	def get_combined_json_data(sdc_name):
		with open("%s/%s.combined.json" % (config_path,sdc_name)) as combined_json:
			json_obj = json.load(combined_json)
			return json_obj

	def find_contract_address(contract,contracts_with_chosen_addresses):
		address = [ e[0] for e in contracts_with_chosen_addresses if e[1] == contract ]
		assert len(set(address)) == 1 # Can't have more than one! Otherwise we will have conflicting same address for different contracts
		return address[0]
			
	def address_as_str(address):
		if isinstance(address,str):
			return address
		return "%0.40x" % (address)
		
	def find_contract_address_str(contract,contracts_with_chosen_addresses):
		address_and_contracts = [ e for e in contracts_with_chosen_addresses if e[1] == contract ]
		address_and_contract = address_and_contracts[0]
		address = address_and_contract[0]
		contract = address_and_contract[1].split(":")[1]
		debug_print("Custom addresses: %s, looking for a match of %s from %s in %s" % (parsed_options[OPTION_ADDRESS],address_and_contract,contract, parsed_options[OPTION_ADDRESS].keys()))
		if contract in parsed_options[OPTION_ADDRESS].keys():
			address = parsed_options[OPTION_ADDRESS][contract]
		debug_print("Candidate addresses for %s is %s" % (contract,address))
		assert len(set(address_and_contracts)) == 1 # Can't have more than one! Otherwise we will have conflicting same address for different contracts
		return address_as_str(address)
		
			
	def collect_and_link_bytecode(contract_name, path, contracts_with_chosen_addresses):
		unlinked_binary = ""
		saw_linker_hints = -1
		linker_hints = {}
		debug_print("Contracts with chosen addresses: %s" % ([("0x%X" % x[0],x[1]) for x in contracts_with_chosen_addresses]))
		
		with open("%s/%s.bin-runtime" % (path, contract_name)) as binary:
			for i,line in enumerate(binary):
				debug_print("Working on line %d - %s" % (i,line))
				if i == 0:
					unlinked_binary = line.strip()
				
				if line.startswith("//"):
					if saw_linker_hints == -1:
						saw_linker_hints = i
						
					# It's a linker hint
					linker_hint = line.replace("//","")
					(handle,contract) = map(lambda x: x.strip(), linker_hint.split("->"))
					debug_print("Got %d linker hint with handle %s and contract %s" % (i-saw_linker_hints, handle, contract))
					if (handle,contract) not in linker_hints:
						linker_hints[(handle,contract)] = 1
					else:
						linker_hints[(handle,contract)] += 1
			
			def find_nth_occurrence(data,string,n):
				splits = data.split(string,maxsplit=n+1)
				if len(splits)<=n+1: # there is no n'th occurrence
					return -1
				# index is received from the full length, minus the length of the string, and minus the last split
				return len(data) - len(string) - len(splits[-1])
			
			# Start to link
			linked_binary = unlinked_binary
			for linker_hint in linker_hints:
				debug_print("Handling linker hint %s with %d occurrences" % (linker_hint, linker_hints[linker_hint]))
				handle = linker_hint[0]
				contract = linker_hint[1]
				for occurrence in reversed(range(linker_hints[linker_hint])): # Go reverse so that occurrence counting stays valid
					occurrenceIdx = find_nth_occurrence(linked_binary,"__%s__" % (handle), occurrence)
					debug_print("Occurrence index of %d is %d" % (occurrence, occurrenceIdx))
					address_to_link_str = find_contract_address_str(contract,contracts_with_chosen_addresses)
					debug_print("Candidate address: %s" % (address_to_link_str))
					linked_binary = "%s%s%s" % (linked_binary[0:occurrenceIdx], address_to_link_str, linked_binary[occurrenceIdx+len("__%s__"%(handle)):])
					debug_print("Current linked binary: %s" % (linked_binary))
					
					library_addresses.append(address_to_link_str)
					
			return linked_binary	
		
	def get_relevant_solc(contract):
		if contract in solc_mappings:
			return solc_mappings[contract]
		else:
			return parsed_options[OPTION_SOLC]
			
	def collect_for_file(file,file_index):
		global library_addresses
		primary_contract = fileToContractName[file]
		sdc_name = "%s_%d" % (file.split("/")[-1],file_index)
		compilation_path = "%s/%s" % (config_path,sdc_name)
		safe_create_dir(compilation_path)
		
		solc_ver_to_run = get_relevant_solc(primary_contract)
		
		# ABI and bin-runtime cmds preparation
		if OPTION_PACKAGES in parsed_options:
			abi_collect_cmd = "%s -o %s/ --overwrite --combined-json abi,hashes,srcmap-runtime%s --allow-paths %s %s %s" % (solc_ver_to_run, config_path, ",local-mappings-runtime" if "varmap" in parsed_options else "", parsed_options[OPTION_PATH], parsed_options[OPTION_PACKAGES], file)
			bin_runtime_collect_cmd = "%s -o %s/ --overwrite --bin-runtime --allow-paths %s %s %s" % (solc_ver_to_run, compilation_path, parsed_options[OPTION_PATH], parsed_options[OPTION_PACKAGES], file)
		else:
			abi_collect_cmd = "%s -o %s/ --overwrite --combined-json abi,hashes,srcmap-runtime%s --allow-paths %s %s" % (solc_ver_to_run, config_path, ",local-mappings-runtime" if "varmap" in parsed_options else "", parsed_options[OPTION_PATH], file)
			bin_runtime_collect_cmd = "%s -o %s/ --overwrite --bin-runtime --allow-paths %s %s" % (solc_ver_to_run, compilation_path, parsed_options[OPTION_PATH], file)
			
		# ABI
		run_cmd(abi_collect_cmd,"%s.abi" % (sdc_name))
			
		# rename combined.json to sdc_name.combined.json
		os.replace("%s/combined.json" % (config_path), "%s/%s.combined.json" % (config_path, sdc_name))
		
		# load data
		data = get_combined_json_data(sdc_name)
		
		contracts = collect_contracts(data) # returns file:contractName
		debug_print("Contracts in %s: %s" % (sdc_name, contracts))
		# 12,14,04,06,00,04,10 is 0xce4604a aka certora. 
		const = (12 * 2**24 + 14 * 2**20 + 4 * 2**16 + 6 * 2**12 + 0 + 4 * 2**4 + 10 * 2**0)
		prefix = const * 2**100 + (file_index+1)*2**16 # allowed up to 2^16-1 libs per file index
		# Don't forget for addresses there are only 160 bits
		contracts_with_chosen_addresses = [(prefix + address,contract) for address,contract in enumerate(contracts)]
		debug_print("Contracts with their chosen addresses: %s" %(contracts_with_chosen_addresses))
		debug_print("Hex 0x%x, prefix is 0x%x" % (const, prefix))
		
		# bin-runtime
		run_cmd(bin_runtime_collect_cmd,"%s.bin-runtime" % (sdc_name))
		
		# srclist - important for parsing source maps
		srclist = collect_srclist(data)
		debug_print("Source list: %s" % (srclist))
		
		contracts_in_sdc = []
		fetched_srclist = {}
		debug_print("finding primary contract address of %s:%s in %s" % (file,primary_contract,contracts_with_chosen_addresses))
		primary_contract_address = find_contract_address_str('%s:%s'%(file,primary_contract),contracts_with_chosen_addresses)
		for contract in contracts:
			contract_name,contract_file = collect_name_file(contract)
			debug_print("Name,File of contract %s: %s, %s" % (contract,contract_name,contract_file))
			funcs = collect_funcs(contract,data)
			debug_print("Functions of %s: %s" % (contract,funcs))
			srcmap = collect_srcmap(contract,data)
			debug_print("Source maps of %s: %s" % (contract,srcmap))

			if "varmap" in parsed_options:
				varmap = collect_varmap(contract,data)
				debug_print("Variable mappings of %s: %s" % (contract,varmap))
			else:
				varmap = ""
			bytecode = collect_and_link_bytecode(contract_name, compilation_path, contracts_with_chosen_addresses)
			debug_print("linked bytecode for %s: %s"% (contract,bytecode))
			address = find_contract_address_str(contract,contracts_with_chosen_addresses)
				
			idx_in_src_list = { v:k for k,v in srclist.items() }[contract_file]
			if "dont_fetch_sources" not in parsed_options:
				# Copy contract_file to compilation path directory
				new_name = "%d_%s.%s" % (idx_in_src_list,get_file_basename(contract_file),get_file_extension(contract_file))
				shutil.copy2(contract_file, '%s/%s' % (compilation_path,new_name))
				fetched_source = '%s/%s' % (sdc_name,new_name)
			else:
				fetched_source = contract_file
				
			fetched_srclist[idx_in_src_list] = fetched_source
		
			contracts_in_sdc.append({"name":contract_name,"original_file":contract_file,"file":fetched_source,"address":address,"methods":funcs,"bytecode":bytecode,"srcmap":srcmap,"varmap":varmap})
		
		# Rebuild srclist for web compatibility
		if "fetched_sources" in parsed_options:
			fetched_srclist = map
		
		debug_print("Contracts in SDC %s: %s" % (sdc_name,contracts_in_sdc))
		# Need to deduplicate the library_addresses list without changing the order
		deduplicated_library_addresses = list(OrderedDict.fromkeys(library_addresses))
		sdc = {"primary_contract":primary_contract,"primary_contract_address":primary_contract_address,"sdc_origin_file":file,"original_srclist":srclist,"srclist":fetched_srclist,"sdc_name":sdc_name,"contracts":contracts_in_sdc,"library_addresses":deduplicated_library_addresses,"generated_with":' '.join(sys.argv)}
		library_addresses = [] # Reset library addresses
		return sdc
	
	def get_sdc_key(contract,address):
		return "%s_%s" % (contract,address)
	
	for i,f in enumerate(files):
		sdc = collect_for_file(f,i)
		
		# First, add library addresses as SDCs too (they should be processed first)
		debug_print("Libraries to add %s" % (sdc["library_addresses"]))
		for library_address in sdc["library_addresses"]:
			library_contract_candidates = [contract for contract in sdc["contracts"] if contract["address"] == library_address]
			assert(len(library_contract_candidates) == 1) # Exactly one result
			
			library_contract = library_contract_candidates[0]
			debug_print("Found library contract %s" % (library_contract))
			# TODO: What will happen with libraries with libraries?
			sdc_lib = {"primary_contract":library_contract["name"],"primary_contract_address":library_address,"sdc_origin_file":library_contract["original_file"],"original_srclist":sdc["original_srclist"],"srclist":sdc["srclist"],"sdc_name":"%s_%s"%(sdc["sdc_name"],library_contract["name"]),"contracts":sdc["contracts"],"library_addresses":[],"generated_with":sdc["generated_with"]}
			SDCs[get_sdc_key(sdc_lib["primary_contract"],sdc_lib["primary_contract_address"])] = sdc_lib
			
		SDCs[get_sdc_key(sdc["primary_contract"],sdc["primary_contract_address"])] = sdc	
		
		
	def has_sdc_name_from_SDCs_starting_with(potential_contract_name):
		candidates = get_matching_sdc_names_from_SDCs(potential_contract_name)
		return candidates
	
	def get_one_sdc_name_from_SDCs(contract):
		return [ k for k,v in SDCs.items() if k.startswith("%s_00000000ce4604a"%(contract))][0]
	
	def get_matching_sdc_names_from_SDCs(contract):
		return [ k for k,v in SDCs.items() if k.startswith("%s_00000000ce4604a"%(contract))]
	
	def resolve_slot(primary_contract,slot_name):
		# TODO: Don't run this command every time
		debug_print("Resolving slots for %s out of %s"%(primary_contract,SDCs.keys()))
		sdc = SDCs[get_one_sdc_name_from_SDCs(primary_contract)] # Enough to pick one
		file = sdc["sdc_origin_file"]
		solc_ver_to_run = get_relevant_solc(primary_contract)
		if OPTION_PACKAGES in parsed_options:
			asm_collect_cmd = "%s -o %s/ --overwrite --asm --allow-paths %s %s %s" % (solc_ver_to_run, config_path, parsed_options[OPTION_PATH], parsed_options[OPTION_PACKAGES], file)
		else:
			asm_collect_cmd = "%s -o %s/ --overwrite --asm --allow-paths %s %s" % (solc_ver_to_run, config_path, parsed_options[OPTION_PATH], file)
		run_cmd(asm_collect_cmd,"%s.asm" % (primary_contract))
		with open("%s/%s.evm" % (config_path,primary_contract),"r") as asm_file:
			debug_print("Got asm %s" % (asm_file))
			saw_match = False
			candidate_slots = []
			for line in asm_file:
				if saw_match:
					candidate_slots.append(line)
					saw_match = False
				else:
					#print(re.search('/\* "[a-zA-Z0-9./_]+":[0-9]+:[0-9]+','/* "contracts/CErc20.sol":5473:5483  underlying */'))
					#regex = '/* "[a-zA-Z0-9./_]+":[0-9]+:[0-9]+ %s */' % (slot_name)
					#regex = ':[0-9]+:[0-9]+ %s ' % (slot_name)
					regex = r'/\* "[a-zA-Z0-9./_\-:]+":[0-9]+:[0-9]+\s* %s \*/' % (slot_name)
					saw_match = re.search(regex, line)
					if saw_match:
						debug_print("Saw match for %s on line %s" % (regex,line))
			debug_print("Candidate slots: %s" % (candidate_slots))
			normalized_candidate_slots = [x.strip() for x in candidate_slots]
			debug_print("Candidate slots: %s" % (normalized_candidate_slots))
			filtered_candidate_slots = [x for x in normalized_candidate_slots if x.startswith("0x")]
			set_candidate_slots = set(filtered_candidate_slots)
			debug_print("Set of candidate slots: %s" % (set_candidate_slots))
			if len(set_candidate_slots) == 1:
				slot_number = int(list(set_candidate_slots)[0],0) # Auto detect base (should be 16 though thanks to 0x)
				debug_print("Got slot number %s" % (slot_number))
			else:
				raise Exception("Failed to resolve slot for %s in %s, valid candidates: %s" % (slot_name,primary_contract,set_candidate_slots))

		return slot_number 
	
	# Important note: Between runs, the basenames of outputs should be the same in order to make the cache checking
	def check_update_cache(output_build, output_verify, output_folder):
		def get_file_digest(filepath):
			hashing = keccak.new(digest_bits=256)
			try:
				with open(filepath,"rb") as file:
					hashing.update(file.read())
					digest = hashing.hexdigest()
					print("filepath %s digest: %s" % (filepath,digest))
					return digest
			except FileNotFoundError:
				print("filepath %s not found" % (filepath))
				return ""
		
		def get_now_string():
			return datetime.now().strftime("%d/%m/%Y %H:%M:%S")
			
		if OPTION_CACHE in parsed_options: # only relevant if caching
			base_cache_path = '%s/cache' % (os.getenv("CERTORA").replace("\\","/"))
			safe_create_dir(base_cache_path) # create base cache (one time thing)
			cache_path = '%s/%s' % (base_cache_path,parsed_options[OPTION_CACHE])
			safe_create_dir(cache_path) # try to create it and will silently fail if already exists
			
			new_build_digest = get_file_digest(output_build)
			old_build_digest = get_file_digest('%s/%s' % (cache_path,get_file_name(output_build)))
			
			new_verify_digest = get_file_digest(output_verify)
			old_verify_digest = get_file_digest('%s/%s' % (cache_path,get_file_name(output_verify)))
			
			# When build results are different, will write a file with the date and time of build
			# After EVMVerifier processes something from cache, it will write a file with "processing time"
			# EVMVerifier can then compare times - if processing time > cache time, will ignore that phase
			# note that if someone deletes the dirty* files, it should behave as if cache_time was '0'
			if new_build_digest != old_build_digest:
				print("Different build %s" % (get_file_name(output_build)))
				# copy new build
				shutil.copy2(output_build,'%s/%s' % (cache_path,get_file_name(output_build)))
				
						
			if new_verify_digest != old_verify_digest:
				print("Different verification query %s" % (get_file_name(output_verify)))
				# copy new vq
				shutil.copy2(output_verify,'%s/%s' % (cache_path,get_file_name(output_verify)))
			
			# Now write "last_..time" anyway
			with open('%s/%s' % (cache_path,LAST_BUILD_TIME),'w+') as dirty_build_time:
				dirty_build_time.write("Build file generation time: %s" % (get_now_string()))

			with open('%s/%s' % (cache_path,LAST_VERIFY_GEN_TIME),'w+') as dirty_verify_gen_time:
				dirty_verify_gen_time.write("Verification query generation time: %s" % (get_now_string()))
			
			# output_folder is copied whenever *anything* changed
			if new_build_digest != old_build_digest or new_verify_digest != old_verify_digest:
				new_config_path = '%s/%s' % (cache_path,get_file_name(output_folder))
				# now copy - using distutil to avoid dangerous rmtree required for shutil to work
				distutils.dir_util.copy_tree(output_folder, new_config_path)
	
	# Link processing
	if OPTION_LINK in parsed_options:
		links = parsed_options[OPTION_LINK]
		for link in links:
			src,dst = link.split("=",2)
			src_contract,src_slot = src.split(":",2)
			if not src_slot.isnumeric():
				# We need to convert the string to a slot number
				resolved_src_slot = resolve_slot(src_contract,src_slot)
			else:
				resolved_src_slot = src_slot
			debug_print("Linking slot %s of %s to %s" %(resolved_src_slot,src_contract,dst))
			debug_print([k for k,v in SDCs.items()])
			# Primary contract name should match here
			if has_sdc_name_from_SDCs_starting_with(dst):
				example_dst = get_one_sdc_name_from_SDCs(dst) # Enough to pick one
				dst_address = SDCs[example_dst]["primary_contract_address"]
			else:
				dst_address = dst # Actually, just a number
				
			sources_to_update = get_matching_sdc_names_from_SDCs(src_contract)
			for source_to_update in sources_to_update:
				if "state" not in SDCs[source_to_update]:
					debug_print("Initializing state of %s" %(source_to_update))
					SDCs[source_to_update]["state"] = {}
				debug_print("Linking %s (%s) to %s in slot %s" % (src_contract,source_to_update,dst_address,resolved_src_slot))
				SDCs[source_to_update]["state"][resolved_src_slot] = dst_address

	#if OPTION_ADDRESS in parsed_options:
	#	manual_addresses = parsed_options[OPTION_ADDRESS]
	#	for address_assignment in manual_addresses:
	#		contract,address = address_assignment.split(":",2)
	#		debug_print("Setting address of %s to %s" %(contract,address))
	#		contracts_to_update = get_matching_sdc_names_from_SDCs(contract)
	#		for contract_to_update in contracts_to_update:
	#			debug_print("Setting address of %s (%s) to address %s" % (contract_to_update, contract, address))
	#			SDCs[contract_to_update]["address"] = address
	
	if OPTION_VERIFY in parsed_options:
		certora_verify_struct = []
		verification_queries = parsed_options[OPTION_VERIFY]
		vq_idx = 0
		for verification_query in verification_queries:
			vq_contract,vq_spec = verification_query.split(":",2)
			# copy vq_spec to config_path
			specname = '%d_%s.spec' % (vq_idx,get_file_basename(vq_spec)) # make sure it is unique with vq_idx
			path_to_specname = '%s/%s' % (CERTORA_CONFIG,specname)
			shutil.copy2(vq_spec, path_to_specname)
			certora_verify_struct.append({"type":"spec","primary_contract":vq_contract,"specfile":path_to_specname})
			vq_idx += 1
		
		with open('%s.json' % (parsed_options[OPTION_OUTPUT_VERIFY]),'w+') as output_file:
			json.dump(certora_verify_struct,output_file,indent=4,sort_keys=True)
	
	# Output	
	if OPTION_OUTPUT in parsed_options: # will never be false because of default
		with open('%s.json' % (parsed_options[OPTION_OUTPUT]),'w+') as output_file:
			json.dump(SDCs,output_file,indent=4,sort_keys=True)
	else:
		print("SDCs:")
		print(SDCs)

	current_output_file = '%s.json' % (parsed_options[OPTION_OUTPUT]) # will never fail because of default
	current_output_folder = parsed_options[OPTION_OUTPUT_FOLDER] # will never fail because of default # But we also do not compare this folder...
	current_output_verify = '%s.json' % (parsed_options[OPTION_OUTPUT_VERIFY]) # will never fail because of default
	# Check and update cache
	check_update_cache(current_output_file, current_output_verify, current_output_folder)
	
except Exception:
	print("Encountered an error configuring the verification environment:")
	print(traceback.format_exc())
	print_usage()
	sys.exit(1)
