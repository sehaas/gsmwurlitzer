from __future__ import print_function
import logging
import serial
import serial.threaded
import threading
import time
from messaging.sms import SmsDeliver, SmsSubmit
from enum import Enum

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

	def __init__(self):
		super(WurlitzerProtocol, self).__init__()
		self.alive = True
		self.playlist = {}
		self.status = Status.IDLE
		self.responses = queue.Queue()
		self.events = queue.Queue()
		self._event_thread = threading.Thread(target=self._run_event)
		self._event_thread.daemon = True
		self._event_thread.name = "wrlz-event"
		self._event_thread.start()
		self.lock = threading.Lock()

	def stop(self):
		self.alive = False
		self.events.put(None)
		self.responses.put('<exit>')

	def _run_event(self):
		while self.alive:
			try:
				self.handle_event(self.events.get())
			except:
				logging.exception('_run_event')

	def init_module(self):
		self.command('ATE0')		# disable echo
		self.command('AT+CFUN=1')	# enable full functionality
		self.command('AT+COLP=0')	# do not block on ATD...
		self.command('AT+CLCC=1')	# report state of current calls
		self.command('AT+CLIP=1')	# indicate incomming call via '+CLIP:...'
		self.command('AT+CMGF=0')	# enable PDU mode for SMS
		self.command('AT+CNMI=2,2')	# handle SMS directly via '+CMT:...'

	def load_playlist(self, path):
		with open(path) as f:
  			self.playlist = dict(l.rstrip().split(None, 1) for l in f)

	def handle_line(self, line):
		print_dbg('DEBUG: ', line)
		if self.status == Status.INCOMING_SMS:
			self.events.put(line)
			return

		if line.startswith('+CMT'):
			self.status = Status.INCOMING_SMS
			# next line is PDU
		elif line.startswith('+CMGS'):
			self.responses.put(line); # last sent SMS-identifier
		elif line.startswith('+COLP'):
			self.status = Status.ACTIVE_CALL
		elif line.startswith('+CLIP'):
			self.status = Status.INCOMING_CALL
			self.handle_call(line)
		elif line.startswith('+'):
			self.events.put(line)
		else:
			self.responses.put(line)

	def handle_event(self, event):
		print_dbg('event received:', event)
		if self.status == Status.INCOMING_SMS:
			self.status = Status.IDLE
			self.handle_sms(event)

	def handle_sms(self, pdu):
		sms = SmsDeliver(pdu)
		print_dbg('SMS from:', sms.number, 'text:', sms.text);
		cmd = sms.text.split(None, 1)[0] # only use first word
		if cmd in self.playlist.keys():
			audio = self.playlist.get(cmd)
			print_dbg('PLAY: ', audio)
		else:
			response = SmsSubmit(sms.number, '> ' + '\n> '.join(self.playlist.keys()))
			resp_pdu = response.to_pdu()[0] # FIXME loop, to support longer SMS
			print_dbg('RESP: ', resp_pdu.pdu)
			# cannot wait for response '> ' due to missing '\r'
			self.command(b'AT+CMGS=%d' % resp_pdu.length, None)
			time.sleep(1) # just wait 1sec instead
			self.command(b'%s\x1a' % resp_pdu.pdu)

	def handle_call(self, line):
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
						print_dbg('WAIT: ', line)
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

