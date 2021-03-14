import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import fnmatch
import OrgExtended.orgparse.node as node
from   OrgExtended.orgparse.sublimenode import * 
import OrgExtended.orgutil.util as util
import OrgExtended.orgutil.navigation as nav
import OrgExtended.orgutil.template as templateEngine
import logging
import sys
import traceback 
import OrgExtended.orgfolding as folding
import OrgExtended.orgdb as db
import OrgExtended.asettings as sets
import OrgExtended.orgcapture as capture
import OrgExtended.orglinks as links
import OrgExtended.orgclocking as clocking
import OrgExtended.orgextension as ext
import OrgExtended.pymitter as evt
import importlib
import tempfile

log = logging.getLogger(__name__)

RE_END = re.compile(r"^\s*\#\+(END_SRC|end_src)")
RE_SRC_BLOCK = re.compile(r"^\s*\#\+(BEGIN_SRC|begin_src)\s+(?P<name>[^: ]+)\s*")
RE_FN_MATCH = re.compile(r"\s*[:]([a-zA-Z0-9-_]+)\s+([^: ]+)\s*")
RE_RESULTS = re.compile(r"^\s*\#\+(RESULTS|results)[:]\s*$")
RE_HEADING = re.compile(r"^[*]+\s+")
RE_PROPERTY_DRAWER = re.compile(r"^\s*[:][a-zA-Z0-9]+[:]\s*$")
RE_END_PROPERTY_DRAWER = re.compile(r"^\s*[:](END|end)[:]\s*$")
RE_BLOCK = re.compile(r"^\s*\#\+(BEGIN_|begin_)[a-zA-Z]+\s+")
RE_END_BLOCK = re.compile(r"^\s*\#\+(END_|end_)[a-zA-Z]+\s+")
RE_IS_BLANK_LINE = re.compile(r"^\s*$")

def IsSourceBlock(view):
	at = view.sel()[0]
	return (view.match_selector(at.begin(),'orgmode.fence.sourceblock') or view.match_selector(at.begin(),'orgmode.sourceblock.content'))

def IsSourceFence(view,row):
	#line = view.getLine(view.curRow())
	line = view.getLine(row)
	return RE_SRC_BLOCK.search(line) or RE_END.search(line)


def ProcessPotentialFileOrgOutput(cmd):
	cmd.outputs = list(filter(None, cmd.outputs)) 
	if(cmd.params and cmd.params.Get('file',None)):
		out = cmd.params.Get('file',None)
		if(hasattr(self,'output') and self.output):
			out = self.output
		if(out):
			sourcepath = os.path.dirname(self.sourcefile)
			destFile    = os.path.join(sourcepath,out)
			destFile = os.path.relpath(destFile, sourcepath)
			self.outputs.append("[[file:" + destFile + "]]")

class OrgExecuteSourceBlockCommand(sublime_plugin.TextCommand):
	def OnDone(self):
		evt.EmitIf(self.onDone)

	def on_replaced(self):
		if(hasattr(self.curmod,"PostExecute")):
			self.curmod.PostExecute(self)

		if(hasattr(self.curmod,"GeneratesImages") and self.curmod.GeneratesImages(self)):
			self.view.run_command("org_cycle_images",{"onDone": evt.Make(self.OnDone)})
		else:
			self.OnDone()

	def end_results(self,rw):
		self.endResults     = rw
		self.resultsStartPt = self.view.text_point(self.startResults+1,0)
		self.resultsEndPt   = self.view.text_point(self.endResults,0)
		self.resultsRegion  = sublime.Region(self.resultsStartPt, self.resultsEndPt)

	def find_results(self,edit):
		row              = self.endRow+1
		fileEndRow,_     = self.view.rowcol(self.view.size())
		inResults        = False
		inPropertyDrawer = False
		inBlock          = False
		for rw in range(row, fileEndRow):
			line = self.view.substr(self.view.line(self.view.text_point(rw,0)))
			if(not inResults and RE_RESULTS.search(line)):
				self.startResults = rw
				inResults = True
				continue
			# A new heading ends the results.
			if(RE_HEADING.search(line)):
				if(inResults):
					self.end_results(rw)
					return True
				else:
					break
			if(inResults and not inPropertyDrawer and RE_PROPERTY_DRAWER.search(line)):
				inPropertyDrawer = True
				continue
			if(inResults and not inBlock and RE_BLOCK.search(line)):
				inBlock = True
				continue
			if(inResults and not inBlock and not inPropertyDrawer and inResults and RE_IS_BLANK_LINE.search(line)):
				self.end_results(rw)
				return True
			if(inPropertyDrawer and RE_END_PROPERTY_DRAWER.search(line)):
				self.end_results(rw)
				return True
			if(inBlock and RE_END_BLOCK.search(line)):
				self.end_results(rw)
				return True
		# We just hit the end of the file.
		if(inResults):
			self.end_results(fileEndRow)
			return True
		# We hit the end of the file and didn't find a results tag.
		# We need to make one.
		if(not inResults):
			log.debug("Could not locate #+RESULTS tag adding one!")
			if(self.endRow == self.view.endRow()):
				pt = self.view.text_point(self.endRow,0)
				pt = self.view.line(pt).end()
			else:
				pt = self.view.text_point(self.endRow,0)
				pt = self.view.line(pt).end() + 1
			indent = db.Get().AtInView(self.view).indent()
			self.view.insert(edit, pt, "\n" +indent+ "#+RESULTS:\n")
			self.startResults   = self.endRow + 2 
			self.endResults     = self.startResults + 1
			self.resultsStartPt = self.view.text_point(self.startResults+1,0)
			self.resultsEndPt   = self.view.text_point(self.endResults,0)
			self.resultsRegion  = sublime.Region(self.resultsStartPt, self.resultsEndPt)
			return True

	def on_warning_saved(self):
		if(self.view.is_dirty()):
			self.view.set_status("Error: ","Failed to save the view. ABORT, cannot execute source block since it is dirty")
			log.error("Failed to save the view. ABORT, cannot execute source code")
			return
		self.view.run_command("org_execute_source_block", {"onDone": self.onDone})

	def run(self, edit, onDone=None):
		self.onDone = onDone
		view = self.view
		at = view.sel()[0]
		if(view.match_selector(at.begin(),'orgmode.fence.sourceblock') or view.match_selector(at.begin(),'orgmode.sourceblock.content')):
			# Scan up till we find the start of the block.
			row = view.curRow()
			while(row > 0):
				if(IsSourceFence(view, row)):
					at = sublime.Region(view.text_point(row,1),view.text_point(row,1))
					break
				row -= 1
			# Okay we have a dynamic block, now we need to know where it ends.
			start = at.begin()
			end   = None
			erow = view.endRow()
			for rw in range(row,erow+1):
				line = view.substr(view.line(view.text_point(rw,0)))
				if(RE_END.search(line)):
					end = rw
					break
			if(not end):
				log.error("Could not locate #+END_SRC tag")
				return

			# Okay now we have a start and end to build a region out of.
			# time to run a command and try to get the output.
			extensions = ext.find_extension_modules('orgsrc', ["plantuml", "graphviz", "ditaa", "powershell", "python", "gnuplot"])
			line = view.substr(view.line(start))
			m = RE_SRC_BLOCK.search(line)
			if(not m):
				log.error("FAILED TO PARSE SOURCE BLOCK: " + line)
				return
			fnname = m.group('name')
			#log.debug("SRC NAME: " + fnname)
			params = util.PList.createPList(line[len(m.group(0)):])
			#paramstr = line[len(m.group(0)):]
			#params = {}
			#for m in RE_FN_MATCH.finditer(paramstr):
			#	params[m.group(1)] = m.group(2)
			# Now find me that function!
			if(fnname not in extensions):
				log.error("Function not found in src folder! Cannot execute!")
				return

			# Start setting up our execution state.
			self.params   = params
			self.curmod   = extensions[fnname]
			self.startRow = row + 1
			self.endRow   = end
			self.s        = view.text_point(self.startRow,0)
			self.e        = view.text_point(self.endRow,0)
			self.region   = sublime.Region(self.s,self.e)
			self.sourcefile = view.file_name()
			# Sanity check that the file exists on disk
			if(not self.sourcefile or not os.path.exists(self.sourcefile)):
				self.view.set_status("Error: ","Your source org file must exist on disk. ABORT.")
				log.error("Your source org file must exist on disk to generate images. The path is used when setting up relative paths.")
				self.OnDone()
				return
			if(view.is_dirty()):
				log.warning("Your source file has unsaved changes. We cannot run source modifications without saving the buffer.")
				view.run_command("save", {"async": False})
				sublime.set_timeout(self.on_warning_saved,1)
				return

			# We need to find and or buid a results block
			# so we can replace it with the results.
			# ORG is super flexible about this, we are NOT!
			self.find_results(edit)

			# Run the "writer"
			if(hasattr(self.curmod,"Execute")):
				# Okay now time to replace the contents of the block
				self.source = view.substr(self.region)
				if(hasattr(self.curmod,"PreProcessSourceFile")):
					self.curmod.PreProcessSourceFile(self)
				if(hasattr(self.curmod,"Extension")):
					tmp = tempfile.NamedTemporaryFile(delete=False,suffix=self.curmod.Extension(self))
					try:
						self.filename = tmp.name
						print(tmp.name)
						if(hasattr(self.curmod,"WrapStart")):
							tmp.write((self.curmod.WrapStart(self) + "\n").encode("ascii"))
						tmp.write(self.source.encode('ascii'))
						if(hasattr(self.curmod,"WrapEnd")):
							tmp.write(("\n" + self.curmod.WrapEnd(self)).encode("ascii"))
						tmp.close()	
						self.outputs = self.curmod.Execute(self,sets)
					finally:
						#os.unlink(tmp.name)
						pass
				else:
					self.filename = None
					self.outputs = self.curmod.Execute(self)
				ProcessPotentialFileOrgOutput(self)
				log.debug("OUTPUT: " + str(self.outputs))
			else:
				log.error("No execute in module, abort")
				return
			# Reformat adding indents to each line!
			# No bad formatting allowed!
			n = db.Get().AtInView(view)
			level = n.level
			indent = "\n " * level + " "
			#outputs = output.split('\n')
			output = indent.join(self.outputs).rstrip()
			self.view.run_command("org_internal_replace", {"start": self.resultsStartPt, "end": self.resultsEndPt, "text": (" " * level + " ") + output+"\n","onDone": evt.Make(self.on_replaced)})
		else:
			log.error("NOT in A Source Block, nothing to run, place cursor on first line of source block")
