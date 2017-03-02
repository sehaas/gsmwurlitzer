from __future__ import print_function
import logging
import serial
import serial.threaded
import threading
import time
import re
from messaging.sms import SmsDeliver, SmsSubmit
from enum import Enum

if os.name == 'posix' and sys.version_info[0] < 3:
	import subprocess32 as subprocess
else:
    import subprocess

try:
	import queue
except ImportError:
	import Queue as queue

class ATException(Exception):
	pass

class Status(Enum):
	IDLE = 0
	INCOMING_SMS = 1
	ACTIVE_CALL = 2
	INCOMING_CALL = 3

def print_dbg(*args):
	print(*args) # uncomment do enable debug
	return

class WurlitzerProtocol(serial.threaded.LineReader):

	TERMINATOR = b'\r\n'
	CLCC_REGEX = re.compile(r'\+CLCC: (\d),(\d),(\d),(\d),(\d),"(.+)",(\d{1,3}),"(.*)"')
	CALL_STATES = ['BUSY', 'RING', 'NO CARRIER', 'NO ANSWER', 'NO DIALTONE']

	def __init__(self):
		super(WurlitzerProtocol, self).__init__()
		self.alive = True
		self.playlist = {}
		self.status = Status.IDLE
		self.responses = queue.Queue()
		self.events = queue.Queue()
		self.clcc_outgoing = queue.Queue()
		self.clcc_incoming = queue.Queue()
		self._event_thread = threading.Thread(target=self.__run_event)
		self._event_thread.daemon = True
		self._event_thread.name = "wrlz-event"
		self._event_thread.start()
		self.lock = threading.Lock()

	def stop(self):
		self.alive = False
		self.events.put(None)
		self.responses.put('<exit>')

	def __run_event(self):
		while self.alive:
			try:
				self.__handle_event(self.events.get())
			except:
				logging.exception('_run_event')

	def init_module(self):
		self.command('ATE0')		# disable echo
		self.command('AT+CFUN=1')	# enable full functionality
		self.command('AT+COLP=0')	# do not block on ATD...
		self.command('AT+CLCC=1')	# report state of current calls
		self.command('AT+CLIP=0')	# do not indicate incomming call via '+CLIP:...'
		self.command('AT+CMGF=0')	# enable PDU mode for SMS
		self.command('AT+CNMI=2,2')	# handle SMS directly via '+CMT:...'

	def load_playlist(self, path):
		with open(path) as f:
  			self.playlist = dict(l.rstrip().split(None, 1) for l in f)

	def handle_line(self, line):
		print_dbg('INPUT: ', line)
		if self.status == Status.INCOMING_SMS:
			self.events.put(line)
			return

		clcc_match = self.CLCC_REGEX.match(line)
		if clcc_match != None and clcc_match.group(2) == '0':
			# outgoing call
			self.clcc_outgoing.put(line)
		elif clcc_match != None and clcc_match.group(2) == '1': 
			# incoming call
			self.clcc_incoming.put(line)
		elif line.startswith('+CMT'):
			# set status; next line is PDU
			self.status = Status.INCOMING_SMS
		elif line.startswith('+CMGS'):
			# last sent SMS-identifier
			self.responses.put(line);
		elif line in self.CALL_STATES:
			print_dbg('ignore call state: ', line)
		elif line.startswith('+'):
			self.events.put(line)
		else:
			self.responses.put(line)

	def __handle_event(self, event):
		print_dbg('event received:', event)
		if self.status == Status.INCOMING_SMS:
			self.status = Status.IDLE
			self.__handle_sms(event)

	def __handle_sms(self, pdu):
		sms = SmsDeliver(pdu)
		print_dbg('SMS from:', sms.number, 'text:', sms.text);
		cmd = sms.text.split(None, 1)[0] # only use first word
		if cmd in self.playlist.keys():
			song = self.playlist.get(cmd)
			print_dbg('PLAY: ', cmd)
			self.__place_call(sms.number, song)
		else:
			print_dbg('SEND PLAYLIST')
			response = SmsSubmit(sms.number, 'Select song:\n> ' + '\n> '.join(self.playlist.keys()))
			for resp_pdu in response.to_pdu():
				print_dbg('RESP:', resp_pdu.pdu)
				# cannot wait for response '> ' due to missing '\r'
				self.command(b'AT+CMGS=%d' % resp_pdu.length, None)
				time.sleep(1) # just wait 1sec instead
				self.command(b'%s\x1a' % resp_pdu.pdu)

	def __place_call(self, number, song):
		print_dbg('Calling: ', number)
		self.command('ATD%s;' % number)
		call_state = 'CALLING'
		timeout_cnt = 3
		player = None
		while True:
			try:
				clcc = self.clcc_outgoing.get(timeout = 10)
				print_dbg('CLCC: ', clcc)
				status = self.CLCC_REGEX.match(clcc).group(3)
				if   status == '0': # ACTIVE
					print_dbg('ACTIVE')
					call_state = 'PLAYING'
					player = subprocess.Popen(['mpg123', '-q', song])
					print_dbg('PLAYING: ', song)
				elif status == '2': # DAILING
					print_dbg('DAILING')
				elif status == '3': # ALERTING (ring?)
					print_dbg('ALERTING')
				elif status == '6': # DISCONNECT
					print_dbg('DISCONNECT')
					if player != None:
						player.kill()
					return
			except queue.Empty:
				print_dbg('queue empty')
				if call_state == 'CALLING':
					timeout_cnt -= 1
					print_dbg('TIMEOUT ', timeout_cnt)
				elif call_state == 'PLAYING':
					if player.poll() != None:
						print_dbg('SONG FINISHED - HANGUP')
						player = None
						self.command('ATH')
					else:
						print_dbg('still playing')
				if timeout_cnt <= 0:
					print_dbg('TIMEOUT - HANGUP')
					self.command('ATH')


	def __handle_call(self, line):
		print_dbg('TODO handle call')

	def command(self, command, response='OK', timeout=10):
		with self.lock:
			self.write_line(command)
			if response is None:
				return
			lines = []
			while True:
				try:
					line = self.responses.get(timeout=timeout)
					if line == response:
						return lines
					else:
						print_dbg('CHECK RESPONSE: ', line)
						lines.append(line)
				except queue.Empty:
					raise ATException('AT command timeout ({!r})'.format(command))

if __name__ == '__main__':
	import time
	ser = serial.serial_for_url('/tmp/ttyV0', baudrate=9600, timeout=1)
	with serial.threaded.ReaderThread(ser, WurlitzerProtocol) as wurlitzer:
		wurlitzer.init_module()
		wurlitzer.load_playlist('playlist.txt')
		wurlitzer.command('AT')
		raw_input('Press Enter to continue')

