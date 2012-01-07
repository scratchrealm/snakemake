# -*- coding: utf-8 -*-

'''
Created on 13.11.2011

@author: Johannes Köster
'''

import re, os, logging, subprocess, glob, inspect
from multiprocessing import Pool
from collections import defaultdict



# Global functions
if "SHELL" in os.environ:
	def _shell(cmd):
		subprocess.check_call(cmd, shell=True, executable = os.environ["SHELL"])
else:
	def _shell(cmd):
		subprocess.check_call(cmd, shell=True)

def shell(cmd, *args, **kwargs):
	variables = dict(globals())
	# add local variables from calling rule/function
	variables.update(inspect.currentframe().f_back.f_locals)
	variables.update(kwargs)
	try:
		_shell(cmd.format(*args, **variables))
	except KeyError as ex:
		raise RuleException("The variable {} is unknown in this context.".format(str(ex)))

class RuleException(Exception):
	pass

class MissingInputException(RuleException):
	def __init__(self):
		self.missing = defaultdict(set)
	
	def add(self, rule, file):
		self.missing[rule].add(file)
		
	def add_all(self, missing):
		for rule, files in missing.items():
			self.missing[rule].update(files)
			
	def __str__(self):
		s = ""
		for rule, files in self.missing.items():
			s += "Missing input files for rule {}:\n{}\n".format(rule, ", ".join(files))
		return s

class AmbiguousRuleException(RuleException):
	def __init__(self, rule1, rule2):
		super(AmbiguousRuleException, self).__init__("Ambiguous rules: {} and {}.".format(rule1, rule2))

class CyclicGraphException(RuleException):
	def __init__(self, rule1, rule2):
		super(AmbiguousRuleException, self).__init__("Cyclic dependency between {} and {}.".format(rule1, rule2))

def run_wrapper(run, rulename, ruledesc, input, output, wildcards):
	"""
	Wrapper around the run method that handles directory creation and output file deletion on error.
	
	Arguments
	run -- the run method
	input -- list of input files
	output -- list of output files
	wildcards -- so far processed wildcards
	"""
	print(ruledesc)

	for o in output:
		dir = os.path.dirname(o)
		if len(dir) > 0 and not os.path.exists(dir):
			os.makedirs(dir)
	try:
		run(input, output, wildcards)
	except (Exception, BaseException) as ex:
		# Remove produced output on exception
		for o in output:
			if os.path.isdir(o): os.rmdir(o)
			elif os.path.exists(o): os.remove(o)
		raise Exception(": ".join((type(ex).__name__,str(ex))))
	for o in output:
		if not os.path.exists(o):
			raise RuleException("Output file {} not produced by rule {}.".format(o, rulename))

class Rule:
	def __init__(self, name):
		"""
		Create a rule
		
		Arguments
		name -- the name of the rule
		"""
		self.name = name
		self.message = None
		self.input = []
		self.output = []
		self.regex_output = []
		self.parents = dict()
		self.wildcard_names = set()
		self.jobs = dict()

	def __to_regex(self, output):
		"""
		Convert a filepath containing wildcards to a regular expression.
		
		Arguments
		output -- the filepath
		"""
		output = re.sub("\.", "\.", output)
		return re.sub('\{(?P<name>\w+?)\}', lambda match: '(?P<{}>.+)'.format(match.group('name')), output)

	def _get_wildcard_names(self, output):
		return set(match.group('name') for match in re.finditer("\{(?P<name>\w+?)\}", output))

	def has_wildcards(self):
		return bool(self.wildcard_names)

	def add_input(self, input):
		"""
		Add a list of input files. Recursive lists are flattened.
		
		Arguments
		input -- the list of input files
		"""
		for item in input:
			if isinstance(item, list): self.add_input(item)
			else: self.input.append(item)

	def add_output(self, output):
		"""
		Add a list of output files. Recursive lists are flattened.
		
		Arguments
		output -- the list of output files
		"""
		for item in output:
			if isinstance(item, list): self.add_output(item)
			else:
				wildcards = self._get_wildcard_names(item)
				if self.output:
					if self.wildcard_names != wildcards:
						raise RuleException("Not all output files of rule {} contain the same wildcards. ".format(self.name))
				else:
					self.wildcard_names = wildcards
				self.output.append(item)
				self.regex_output.append(self.__to_regex(item))

	def set_message(self, message):
		"""
		Set the message that is displayed when rule is executed.
		"""
		self.message = message
	
	def _expand_wildcards(self, requested_output):
		""" Expand wildcards depending on the requested output. """
		if requested_output:
			wildcards = self.get_wildcards(requested_output)
		else:
			return tuple(self.input), tuple(self.output), dict()

		try:
			input = tuple(i.format(**wildcards) for i in self.input)
			output = tuple(o.format(**wildcards) for o in self.output)
			return input, output, wildcards
		except KeyError:
			raise RuleException("Could not resolve wildcard in rule {}: {}".format(self.name, i))

	def _get_missing_files(self, files):
		""" Return the tuple of files that are missing form the given ones. """
		return tuple(f for f in files if not os.path.exists(f))
	
	def _has_missing_files(self, files):
		""" Return True if any of the given files does not exist. """
		for f in files:
			if not os.path.exists(f):
				return True
		return False

	def _check_missing_input(self, input):
		missing_input = self._get_missing_files(input)
		if missing_input: 
			raise MissingInputException()
	
	def _to_visit(self, input):
		for rule in workflow.get_rules():
			if rule != self:
				for i in input:
					if rule.is_producer(i):
						yield rule, i
					
	def check_dag(self, requested_output = None, forceall = False, visited = set(), jobs = set()):
		if (self, requested_output) in visited:
			raise CyclicGraphException(self)
		visited.add((self, requested_output))
		input, output, _ = self._expand_wildcards(requested_output)
		
		if output and output in jobs:
			return
		
		missing_input = defaultdict(list)
		producer = dict()
		for rule, file in self._to_visit(input):
			try:
				rule.check_dag(file, forceall = forceall, visited = set(visited))
				if file in producer:
					raise AmbiguousRuleException(producer[file], rule)
				producer[file] = rule
			except MissingInputException as ex:
				missing_input[file].append(ex.missing)
		
		missing_input_ex = None
		for i in self._get_missing_files(input):
			if i not in producer:
				if not missing_input_ex:
					missing_input_ex = MissingInputException()
				if i in missing_input:
					for m in missing_input[i]:
						missing_input_ex.add_all(m)
				else:
					missing_input_ex.add(self, i)
		if missing_input_ex:
			raise missing_input_ex
		
		jobs.add(output)

	def dryrun(self, requested_output = None, forceall = False, forcethis = False, jobs = set()):		
		input, output, wildcards = self._expand_wildcards(requested_output)
		
		if output and output in jobs:
			return False
		
		any_run = False
		produced = set()
		for rule, file in self._to_visit(input):
			try:
				any_run |= rule.dryrun(file, forceall = forceall, jobs = jobs)
				produced.add(file)
			except MissingInputException:
				continue
		
		self._check_missing_input(set(input) - produced)
		
		if self._need_run(forcethis or forceall or any_run, input, output):
			print(self.get_message(input, output, wildcards))
			jobs.add(output)
			return True
		return False
	
	def run(self, requested_output = None, forceall = False, forcethis = False, jobs = dict()):
		input, output, wildcards = self._expand_wildcards(requested_output)
		
		if output and output in jobs:
			return jobs[output]
		
		todo = []
		for rule, file in self._to_visit(input):
			try:
				todo.append(rule.run(file, forceall = forceall, jobs = jobs))
			except MissingInputException:
				continue
		
		for job in todo:
			if job:	job.get()
		
		self._check_missing_input(input)
		
		if self._need_run(forcethis or forceall, input, output):
			job = workflow.get_pool().apply_async(
					run_wrapper, 
					[self._get_run(), self.name, 
					self.get_message(input, output, wildcards), input, output, wildcards]
					)
			jobs[output] = job
			return job

	def check(self):
		if self.output and not self.has_run():
			raise RuleException("Rule {} defines output but does not have a \"run\" definition.".format(self.name))

	def _is_queued(self, output, jobs):
		""" Return True if a job for the requested output is already queued. """
		return output in jobs

	def _need_run(self, force, input, output):
		""" Return True if rule needs to be run. """
		if self.has_run():
			if force:
				return True
			if self._has_missing_files(output):
				return True
			mintime = min(map(lambda f: os.stat(f).st_mtime, output))
			for i in input:
				if os.path.exists(i) and os.stat(i).st_mtime >= mintime: 
					return True
		return False

	def _get_run(self):
		return globals()["__" + self.name]

	def has_run(self):
		return "__" + self.name in globals()

	def get_message(self, input, output, wildcards, showmessage = True):
		if self.message and showmessage:
			variables = dict(globals())
			variables.update(locals())
			return self.message.format(**variables)
		return "rule {}:\n\tinput: {}\n\toutput: {}\n".format(
			self.name, ", ".join(input), ", ".join(output))

	def is_parent(self, rule):
		return self in rule.parents.values()

	def is_producer(self, requested_output):
		"""
		Returns True if this rule is a producer of the requested output.
		"""
		for o in self.regex_output:
			match = re.match(o, requested_output)
			if match and len(match.group()) == len(requested_output):
				return True
		return False
			
	def _wildcards_to_str(self, wildcards):
		if wildcards:
			return "Wildcards:\n" + "\n".join(": ".join(i) for i in wildcards.items())
		return ""

	def get_wildcards(self, requested_output):
		"""
		Update the given wildcard dictionary by matching regular expression output files to the requested concrete ones.
		
		Arguments
		wildcards -- a dictionary of wildcards
		requested_output -- a concrete filepath
		"""
		bestmatchlen = 0
		bestmatch = None
		for o in self.regex_output:
			match = re.match(o, requested_output)
			if match and len(match.group()) == len(requested_output):
				l = self.get_wildcard_len(match.groupdict())
				if not bestmatch or bestmatchlen > l:
					bestmatch = match.groupdict()
					bestmatchlen = l
		return bestmatch
		
	
	def get_wildcard_len(self, wildcards):
		return sum(map(len, wildcards.values()))

	def partition_output(self, requested_outputs):
		partition = defaultdict(list)
		for r in requested_outputs:
			wc = frozenset(self.get_wildcards(r).items())
			partition[wc].append(r)
		return partition.values()

	def __repr__(self):
		return self.name

class Workflow:

	def __init__(self):
		"""
		Create the controller.
		"""
		self.__rules = dict()
		self.__last = None
		self.__first = None
		self.__workdir_set = False

	def setup_pool(self, jobs):
		self.__pool = Pool(processes=jobs)
	
	def get_pool(self):
		"""
		Return the current thread pool.
		"""
		return self.__pool
	
	def add_rule(self, rule):
		"""
		Add a rule.
		"""
		self.__rules[rule.name] = rule
		self.__last = rule
		if not self.__first:
			self.__first = rule
			
	def is_rule(self, name):
		"""
		Return True if name is the name of a rule.
		
		Arguments
		name -- a name
		"""
		return name in self.__rules

	def get_rule(self, name):
		"""
		Get rule by name.
		
		Arguments
		name -- the name of the rule
		"""
		return self.__rules[name]

	def last_rule(self):
		"""
		Return the last rule.
		"""
		return self.__last

	def run_first_rule(self, dryrun = False, forcethis = False, forceall = False):
		"""
		Apply the rule defined first.
		"""
		if self.__first.has_wildcards():
			raise RuleException("First rule must not contain any wildcard")

		self.__first.check_dag()
		if dryrun:
			self.__first.dryrun(forcethis = forcethis, forceall = forceall)
		else:
			job = self.__first.run(forcethis = forcethis, forceall = forceall)
			if job: job.get()		
		
	def run_rule(self, name, dryrun = False, forcethis = False, forceall = False):
		"""
		Apply a rule.
		
		Arguments
		name -- the name of the rule to apply
		"""
		rule = self.__rules[name]
		if rule.has_wildcards():
			raise RuleException("Only rules without wildcards may be run directly from command line")
		if dryrun:
			rule.dryrun(forcethis = forcethis, forceall = forceall)
		else:
			job = rule.run(forcethis = forcethis, forceall = forceall)
			if job: job.get()

	def check_rules(self):
		"""
		Check all rules.
		"""
		for rule in self.get_rules():
			rule.check()

	def get_rules(self):
		"""
		Get the list of rules.
		"""
		return self.__rules.values()

	def is_produced(self, files):
		"""
		Return True if files are already produced.
		
		Arguments
		files -- files to check
		"""
		for f in files:
			if not os.path.exists(f): return False
		return True
	
	def is_newer(self, files, time):
		"""
		Return True if files are newer than a time
		
		Arguments
		files -- files to check
		time -- a time
		"""
		for f in files:
			if os.stat(f).st_mtime > time: return True
		return False

	def execdsl(self, compiled_dsl_code):
		"""
		Execute a piece of compiled snakemake DSL.
		"""
		exec(compiled_dsl_code, globals())

	def set_workdir(self, workdir):
		if not self.__workdir_set:
			if not os.path.exists(workdir):
				os.makedirs(workdir)
			os.chdir(workdir)
			self.__workdir_set = True

workflow = Workflow()

def _set_workdir(path):
	workflow.set_workdir(path)

def _add_rule(name):
	if workflow.is_rule(name):
		raise SyntaxError("The name {} is already used by another rule".format(name))
	if "__" + name in globals():
		raise SyntaxError("The name __{} is already used by a variable.".format(name))
	workflow.add_rule(Rule(name))

def _set_input(paths):
	workflow.last_rule().add_input(paths)

def _set_output(paths):
	workflow.last_rule().add_output(paths)

def _set_message(message):
	workflow.last_rule().set_message(message)
