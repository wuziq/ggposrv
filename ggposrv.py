#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# open source ggpo server (re)implementation
#
#  (c) 2014 Pau Oliva Fora (@pof)
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# ggposrv.py includes portions of code borrowed from hircd.
# hircd is Copyright by Ferry Boender, 2009-2013
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#

import sys
import optparse
import logging
import ConfigParser
import os
import SocketServer
import socket
import select
import re
import struct

class GGPOError(Exception):
	"""
	Exception thrown by GGPO command handlers to notify client of a server/client error.
	"""
	def __init__(self, code, value):
		self.code = code
		self.value = value

	def __str__(self):
		return repr(self.value)

class GGPOChannel(object):
	"""
	Object representing an GGPO channel.
	"""
	def __init__(self, name, rom, topic, motd='Welcome to the unofficial GGPO server.\n\nThanks Papasi.\n'):
		self.name = name
		self.rom = rom
		self.topic = topic
		self.motd = motd
		self.clients = set()

class GGPOClient(SocketServer.BaseRequestHandler):
	"""
	GGPO client connect and command handling. Client connection is handled by
	the `handle` method which sets up a two-way communication with the client.
	It then handles commands sent by the client by dispatching them to the
	handle_ methods.
	"""
	def __init__(self, request, client_address, server):
		self.nick = None		# Client's currently registered nickname
		self.host = client_address	# Client's hostname / ip.
		self.status = 0			# Client's status
		self.opponent = None		# Client's opponent
		self.port = 6009		# Client's port
		self.city = "null"		# Client's city
		self.country = "null"		# Client's country
		self.cc = "null"			# Client's country code
		self.password = None		# Client's entered password
		self.send_queue = []		# Messages to send to client (strings)
		self.channel = GGPOChannel("lobby",'', "The Lobby")	# Channel the client is in

		SocketServer.BaseRequestHandler.__init__(self, request, client_address, server)

	def pad2hex(self,l):
		return "".join(reversed(struct.pack('I',l)))

	def sizepad(self,value):
		if value==None:
			return('')
		l=len(value)
		pdu = self.pad2hex(l)
		pdu += value
		return pdu

	def reply(self,sequence,pdu):

		length=4+len(pdu)
		return self.pad2hex(length) + self.pad2hex(sequence) + pdu

	def parse(self, data):

		response = ''
		logging.debug('from %s: %r' % (self.client_ident(), data))

		length=int(data[0:4].encode('hex'),16)
		if (len(data)<length-4): return()
		sequence=0

		if (length >= 4):
			sequence=int(data[4:8].encode('hex'),16)

		if (length >= 8):
			command=int(data[8:12].encode('hex'),16)
			if (command==0):
				command = "connect"
				params = sequence

			if (command==1):
				command = "auth"
				nicklen=int(data[12:16].encode('hex'),16)
				nick=data[16:16+nicklen]
				passwordlen=int(data[16+nicklen:16+nicklen+4].encode('hex'),16)
				password=data[20+nicklen:20+nicklen+passwordlen]
				port=int(data[20+nicklen+passwordlen:24+nicklen+passwordlen].encode('hex'),16)
				params=nick,password,port,sequence

			if (command==2):
				if self.nick==None: return()
				command = "motd"
				params = sequence

			if (command==3):
				if self.nick==None: return()
				command="list"
				params = sequence

			if (command==4):
				if self.nick==None: return()
				command="users"
				params = sequence

			if (command==5):
				if self.nick==None: return()
				command="join"
				channellen=int(data[12:16].encode('hex'),16)
				channel=data[16:16+channellen]
				params = channel,sequence

			if (command==6):
				if self.nick==None: return()
				command="status"
				status=int(data[12:16].encode('hex'),16)
				params = status,sequence

			if (command==7):
				if self.nick==None: return()
				command="privmsg"
				msglen=int(data[12:16].encode('hex'),16)
				msg=data[16:16+msglen]
				params = msg,sequence

			logging.info('NICK: %s SEQUENCE: %d COMMAND: %s' % (self.nick,sequence,command))

			try:
				handler = getattr(self, 'handle_%s' % (command), None)
				if not handler:
					logging.info('No handler for command: %s. Full line: %r' % (command, data))
					if self.nick==None: return()
					command="unknown"
					params = sequence
					handler = getattr(self, 'handle_%s' % (command), None)

				response = handler(params)
			except AttributeError, e:
				raise e
				logging.error('%s' % (e))
			except GGPOError, e:
				response = ':%s %s %s' % (self.server.servername, e.code, e.value)
				logging.error('%s' % (response))
			except Exception, e:
				response = ':%s ERROR %s' % (self.server.servername, repr(e))
				logging.error('%s' % (response))
				raise

		if (len(data) > length+4 ):
			pdu=data[length+4:]
			self.parse(pdu)

		return response

	def handle(self):
		logging.info('Client connected: %s' % (self.client_ident(), ))

		while True:
			buf = ''
			ready_to_read, ready_to_write, in_error = select.select([self.request], [], [], 0.1)

			# Write any commands to the client
			while self.send_queue:
				msg = self.send_queue.pop(0)
				#logging.debug('to %s: %r' % (self.client_ident(), msg))
				self.request.send(msg)

			# See if the client has any commands for us.
			if len(ready_to_read) == 1 and ready_to_read[0] == self.request:
				data = self.request.recv(1024)

				if not data:
					break
				elif len(data) > 0:
					response = self.parse(data)

					if response:
						logging.debug('<<<<<<>>>>>to %s: %r' % (self.client_ident(), response))
						self.request.send(response)

		self.request.close()

	def handle_unknown(self, params):
		sequence = params
		response = self.reply(sequence,'\x00\x00\x00\x08')
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

	def handle_connect(self, params):
		sequence = params
		response = self.reply(sequence,'\x00\x00\x00\x00')
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

	def handle_motd(self, params):
		sequence = params

		pdu='\x00\x00\x00\x00'

		channel = self.channel

		pdu+=self.sizepad(channel.name)
		pdu+=self.sizepad(channel.topic)
		pdu+=self.sizepad(channel.motd)

		response = self.reply(sequence,pdu)
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

	def handle_auth(self, params):
		"""
		Handle the initial setting of the user's nickname
		"""
		nick,password,port,sequence = params

		# New connection
		if nick in self.server.clients:
			# Someone else is using the nick
			# auth unsuccessful
			response = self.reply(sequence,'\x00\x00\x00\x06')
			logging.debug('to %s: %r' % (self.client_ident(), response))
			self.send_queue.append(response)
			self.finish()

		else:
			# Nick is available, register.
			logging.info('NICK: %s PORT: %d' % (nick,port))
			self.nick = nick
			self.server.clients[nick] = self
			self.port = port
			self.password = password

			# auth successful
			response = self.reply(sequence,'\x00\x00\x00\x00')
			logging.debug('to %s: %r' % (self.client_ident(), response))
			self.send_queue.append(response)

			negseq=4294967293 #'\xff\xff\xff\xfd'
			pdu='\x00\x00\x00\x02'
			pdu+='\x00\x00\x00\x01'
			pdu+=self.sizepad(self.nick)
			pdu+=self.pad2hex(self.status) #status
			pdu+='\x00\x00\x00\x00' #p2(?)
			pdu+=self.sizepad(str(self.host[0]))
			pdu+='\x00\x00\x00\x00' #unk1
			pdu+='\x00\x00\x00\x00' #unk2
			pdu+=self.sizepad(self.city)
			pdu+=self.sizepad(self.cc)
			pdu+=self.sizepad(self.country)
			pdu+=self.pad2hex(self.port)      # port
			pdu+='\x00\x00\x00\x01' # ?
			pdu+=self.sizepad(nick)
			pdu+=self.pad2hex(self.status) #status
			pdu+='\x00\x00\x00\x00' #p2(?)
			pdu+=self.sizepad(str(self.host[0]))
			pdu+='\x00\x00\x00\x00' #unk1
			pdu+='\x00\x00\x00\x00' #unk2
			pdu+=self.sizepad(self.city)
			pdu+=self.sizepad(self.cc)
			pdu+=self.sizepad(self.country)
			pdu+=self.pad2hex(self.port)      # port

			response = self.reply(negseq,pdu)
			logging.debug('to %s: %r' % (self.client_ident(), response))
			self.send_queue.append(response)


	def handle_status(self, params):
		status,sequence = params
		self.status = status

		# send ack to the client
		response = self.reply(sequence,'\x00\x00\x00\x00')
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

		negseq=4294967293 #'\xff\xff\xff\xfd'
		pdu='\x00\x00\x00\x01'
		pdu+='\x00\x00\x00\x01'
		pdu+=self.sizepad(self.nick)
		pdu+=self.pad2hex(self.status) #status
		pdu+='\x00\x00\x00\x00' #p2(?)
		pdu+=self.sizepad(str(self.host[0]))
		pdu+='\x00\x00\x00\x00' #unk1
		pdu+='\x00\x00\x00\x00' #unk2
		pdu+=self.sizepad(self.city)
		pdu+=self.sizepad(self.cc)
		pdu+=self.sizepad(self.country)
		pdu+=self.pad2hex(self.port)      # port

		response = self.reply(negseq,pdu)

		for client in self.channel.clients:
			# Send message to all client in the channel
			logging.debug('to %s: %r' % (client.client_ident(), response))
			client.send_queue.append(response)

	def handle_users(self, params):

		sequence = params
		pdu=''
		i=0

		for client in self.channel.clients:
			i=i+1

			pdu+=self.sizepad(client.nick)
			pdu+=self.pad2hex(client.status) #status
			if (client.opponent!=None):
				pdu+=self.sizepad(client.opponent)
			else:
				pdu+='\x00\x00\x00\x00'

			pdu+=self.sizepad(str(client.host[0]))
			pdu+='\x00\x00\x00\x00' #unk1
			pdu+='\x00\x00\x00\x00' #unk2
			pdu+=self.sizepad(client.city)
			pdu+=self.sizepad(client.cc)
			pdu+=self.sizepad(client.country)
			pdu+=self.pad2hex(client.port)      # port

		response = self.reply(sequence,'\x00\x00\x00\x00'+self.pad2hex(i)+pdu)
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

	def handle_list(self, params):

		sequence = params

		pdu=''
		i=0
		for target in self.server.channels:
			i=i+1
			channel = self.server.channels.get(target)
			pdu+=self.sizepad(channel.name)
			pdu+=self.sizepad(channel.rom)
			pdu+=self.sizepad(channel.topic)
			pdu+=self.pad2hex(i)
		
		response = self.reply(sequence,'\x00\x00\x00\x00'+self.pad2hex(i)+pdu)
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

	def handle_join(self, params):
		"""
		Handle the JOINing of a user to a channel.
		"""

		channel_name,sequence = params

		if not channel_name in self.server.channels or self.nick==None:
			# send the NOACK to the client
			response = self.reply(sequence,'\x00\x00\x00\x08')
			logging.debug('JOIN NO_ACK to %s: %r' % (self.client_ident(), response))
			self.send_queue.append(response)
			return()

		# part from previously joined channel
		self.handle_part(self.channel.name)

		# Add user to the channel (create new channel if not exists)
		channel = self.server.channels.setdefault(channel_name, GGPOChannel(channel_name, channel_name, channel_name))
		channel.clients.add(self)

		# Add channel to user's channel list
		self.channel = channel

		# send the ACK to the client
		response = self.reply(sequence,'\x00\x00\x00\x00')
		logging.debug('JOIN ACK to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

		negseq=4294967295 #'\xff\xff\xff\xff'
		response = self.reply(negseq,'')
		logging.debug('CONNECITON ESTABLISHED to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)


		negseq=4294967293 #'\xff\xff\xff\xfd'
		pdu='\x00\x00\x00\x01'
		pdu+='\x00\x00\x00\x01'
		pdu+=self.sizepad(self.nick)
		pdu+=self.pad2hex(self.status) #status
		pdu+='\x00\x00\x00\x00' #p2(?)
		pdu+=self.sizepad(str(self.host[0]))
		pdu+='\x00\x00\x00\x00' #unk1
		pdu+='\x00\x00\x00\x00' #unk2
		pdu+=self.sizepad(self.city)
		pdu+=self.sizepad(self.cc)
		pdu+=self.sizepad(self.country)
		pdu+=self.pad2hex(self.port)      # port

		response = self.reply(negseq,pdu)

		for client in channel.clients:
			client.send_queue.append(response)
			logging.debug('CLIENT JOIN to %s: %r' % (client.client_ident(), response))

	def handle_privmsg(self, params):
		"""
		Handle sending a message to a channel.
		"""
		msg, sequence = params

		channel = self.channel

		# send the ACK to the client
		response = self.reply(sequence,'\x00\x00\x00\x00')
		logging.debug('to %s: %r' % (self.client_ident(), response))
		self.send_queue.append(response)

		for client in channel.clients:
			# Send message to all client in the channel
			negseq=4294967294 #'\xff\xff\xff\xfe'
			response = self.reply(negseq,self.sizepad(self.nick)+self.sizepad(msg))
			logging.debug('to %s: %r' % (client.client_ident(), response))
			client.send_queue.append(response)

	def handle_part(self, params):
		"""
		Handle a client parting from channel(s).
		"""
		pchannel = params
		# Send message to all clients in the channel user is in, and
		# remove the user from the channel.
		channel = self.server.channels.get(pchannel)

		negseq=4294967293 #'\xff\xff\xff\xfd'
		pdu=''
		pdu+='\x00\x00\x00\x01' #unk1
		pdu+='\x00\x00\x00\x00' #unk2
		pdu+=self.sizepad(self.nick)

		response = self.reply(negseq,pdu)

		for client in self.channel.clients:
			if client != self:
				# Send message to all client in the channel except ourselves
				logging.debug('to %s: %r' % (client.client_ident(), response))
				client.send_queue.append(response)

		if self in channel.clients:
			channel.clients.remove(self)

	def handle_dump(self, params):
		"""
		Dump internal server information for debugging purposes.
		"""
		print "Clients:", self.server.clients
		for client in self.server.clients.values():
			print " ", client
			print "     ", client.channel.name
		print "Channels:", self.server.channels
		for channel in self.server.channels.values():
			print " ", channel.name, channel
			for client in channel.clients:
				print "     ", client.nick, client

	def client_ident(self):
		"""
		Return the client identifier as included in many command replies.
		"""
		return('%s@%s' % (self.nick, self.host[0]))

	def finish(self,response=None):
		"""
		The client conection is finished. Do some cleanup to ensure that the
		client doesn't linger around in any channel or the client list, in case
		the client didn't properly close the connection.
		"""
		logging.info('Client disconnected: %s' % (self.client_ident()))
		if response == None:

			negseq=4294967293 #'\xff\xff\xff\xfd'
			pdu=''
			pdu+='\x00\x00\x00\x01' #unk1
			pdu+='\x00\x00\x00\x00' #unk2
			pdu+=self.sizepad(self.nick)

			response = self.reply(negseq,pdu)

		if self in self.channel.clients:
			# Client is gone without properly QUITing or PARTing this
			# channel.
			for client in self.channel.clients:
				client.send_queue.append(response)
				logging.debug('to %s: %r' % (client.client_ident(), response))
			self.channel.clients.remove(self)
		if self.nick in self.server.clients:
			self.server.clients.pop(self.nick)
		logging.info('Connection finished: %s' % (self.client_ident()))

	def __repr__(self):
		"""
		Return a user-readable description of the client
		"""
		return('<%s %s@%s>' % (
			self.__class__.__name__,
			self.nick,
			self.host[0],
			)
		)

class GGPOServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
	daemon_threads = True
	allow_reuse_address = True

	def __init__(self, server_address, RequestHandlerClass):
		self.servername = 'localhost'
		self.channels = {} # Existing channels (GGPOChannel instances) by channelname
		self.channels['ssf2t']=GGPOChannel("ssf2t", "ssf2t", "Super Street Fighter II: Turbo")
		self.channels['game1']=GGPOChannel("game1", "game1", "Game one")
		self.channels['game2']=GGPOChannel("game2", "game2", "Game two")
		self.channels['xmcota']=GGPOChannel("xmcota", "xmcota", "X m cota")
		self.channels['lobby']=GGPOChannel("lobby", '', "The Lobby")
		self.clients = {}  # Connected clients (GGPOClient instances) by nickname
		SocketServer.TCPServer.__init__(self, server_address, RequestHandlerClass)

class Daemon:
	"""
	Daemonize the current process (detach it from the console).
	"""

	def __init__(self):
		# Fork a child and end the parent (detach from parent)
		try:
			pid = os.fork()
			if pid > 0:
				sys.exit(0) # End parent
		except OSError, e:
			sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
			sys.exit(-2)

		# Change some defaults so the daemon doesn't tie up dirs, etc.
		os.setsid()
		os.umask(0)

		# Fork a child and end parent (so init now owns process)
		try:
			pid = os.fork()
			if pid > 0:
				try:
					f = file('ggposrv.pid', 'w')
					f.write(str(pid))
					f.close()
				except IOError, e:
					logging.error(e)
					sys.stderr.write(repr(e))
				sys.exit(0) # End parent
		except OSError, e:
			sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
			sys.exit(-2)

		# Close STDIN, STDOUT and STDERR so we don't tie up the controlling
		# terminal
		for fd in (0, 1, 2):
			try:
				os.close(fd)
			except OSError:
				pass

if __name__ == "__main__":
	#
	# Parameter parsing
	#
	parser = optparse.OptionParser()
	parser.set_usage(sys.argv[0] + " [option]")

	parser.add_option("--start", dest="start", action="store_true", default=True, help="Start ggposrv (default)")
	parser.add_option("--stop", dest="stop", action="store_true", default=False, help="Stop ggposrv")
	parser.add_option("--restart", dest="restart", action="store_true", default=False, help="Restart ggposrv")
	parser.add_option("-a", "--address", dest="listen_address", action="store", default='0.0.0.0', help="IP to listen on")
	parser.add_option("-p", "--port", dest="listen_port", action="store", default='7000', help="Port to listen on")
	parser.add_option("-V", "--verbose", dest="verbose", action="store_true", default=False, help="Be verbose (show lots of output)")
	parser.add_option("-l", "--log-stdout", dest="log_stdout", action="store_true", default=False, help="Also log to stdout")
	parser.add_option("-e", "--errors", dest="errors", action="store_true", default=False, help="Do not intercept errors.")
	parser.add_option("-f", "--foreground", dest="foreground", action="store_true", default=False, help="Do not go into daemon mode.")

	(options, args) = parser.parse_args()

	# Paths
	configfile = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])),'ggposrv.ini')
	logfile = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])),'ggposrv.log')

	#
	# Logging
	#
	if options.verbose:
		loglevel = logging.DEBUG
	else:
		loglevel = logging.WARNING

	log = logging.basicConfig(
		level=loglevel,
		format='%(asctime)s:%(levelname)s:%(message)s',
		filename=logfile,
		filemode='a')

	#
	# Handle start/stop/restart commands.
	#
	if options.stop or options.restart:
		pid = None
		try:
			f = file('ggposrv.pid', 'r')
			pid = int(f.readline())
			f.close()
			os.unlink('ggposrv.pid')
		except ValueError, e:
			sys.stderr.write('Error in pid file `ggposrv.pid`. Aborting\n')
			sys.exit(-1)
		except IOError, e:
			pass

		if pid:
			os.kill(pid, 15)
		else:
			sys.stderr.write('ggposrv not running or no PID file found\n')

		if not options.restart:
			sys.exit(0)

	logging.info("Starting ggposrv")
	logging.debug("configfile = %s" % (configfile))
	logging.debug("logfile = %s" % (logfile))

	if options.log_stdout:
		console = logging.StreamHandler()
		formatter = logging.Formatter('[%(levelname)s] %(message)s')
		console.setFormatter(formatter)
		console.setLevel(logging.DEBUG)
		logging.getLogger('').addHandler(console)

	if options.verbose:
		logging.info("We're being verbose")

	#
	# Go into daemon mode
	#
	if not options.foreground:
		Daemon()

	#
	# Start server
	#
	try:
		ggposerver = GGPOServer((options.listen_address, int(options.listen_port)), GGPOClient)
		logging.info('Starting ggposrv on %s:%s' % (options.listen_address, options.listen_port))
		ggposerver.serve_forever()
	except socket.error, e:
		logging.error(repr(e))
		sys.exit(-2)
