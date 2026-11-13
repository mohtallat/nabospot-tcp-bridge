import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timezone
from zento_live_commands import LiveCommandChannel
from zento_command_ledger import CommandLedger
from zento_protocol import build_frame

class Registry:
    def __init__(self):
        self.state = 'open'
        self.generation = 2
    def snapshot(self, identity):
        return {'connected': True, 'online': True, 'generation': self.generation,
                'barrier_state': self.state, 'last_seen_at': datetime.now(timezone.utc).isoformat()}

class Socket:
    def __init__(self): self.sent=[]
    def sendall(self, frame): self.sent.append(frame)

class LiveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry=Registry()
        self.path=str(Path(self.tmp.name)/'ledger.db')
        self.channel=LiveCommandChannel(self.registry,serial='6003791910',identity='pilot',address=0,ledger_path=self.path,enabled=True)
        self.request={'type':'movement','serial':'6003791910','address':0,'command':'close','command_id':'close-00000001'}
    def test_disabled(self):
        self.channel.enabled=False
        self.assertEqual(self.channel.submit(self.request)['code'],'physical_execution_disabled')
    def test_wrong_target(self):
        self.assertEqual(self.channel.submit({**self.request,'address':1})['code'],'target_mismatch')
    def test_no_fresh_status(self):
        self.registry.state='unknown'
        self.assertEqual(self.channel.submit(self.request)['code'],'unsafe_or_unknown_state')
    def test_real_frame_and_duplicate_not_replayed(self):
        result=[]
        thread=threading.Thread(target=lambda: result.append(self.channel.submit(self.request)))
        thread.start()
        import time
        for _ in range(200):
            if not self.channel.queue.empty(): break
            time.sleep(.005)
        sock=Socket()
        self.assertTrue(self.channel.process_one(sock,'pilot',2,None,lambda c,p,expected: True))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sock.sent,[build_frame('close',address=0,reversed_direction=True)])
        self.assertEqual(result[0]['code'],'status_confirmed')
        self.assertEqual(self.channel.submit(self.request)['code'],'duplicate')
        self.assertEqual(len(sock.sent),1)
        with CommandLedger(self.path) as ledger:
            self.assertEqual(ledger.get('close-00000001')['state'],'completed')
    def test_unknown_outcome_never_replayed(self):
        result=[]
        thread=threading.Thread(target=lambda: result.append(self.channel.submit(self.request)))
        thread.start()
        import time
        for _ in range(200):
            if not self.channel.queue.empty(): break
            time.sleep(.005)
        sock=Socket()
        self.channel.process_one(sock,'pilot',2,None,lambda c,p,expected: False)
        thread.join(2)
        self.assertEqual(result[0]['code'],'outcome_unknown')
        self.assertEqual(self.channel.submit(self.request)['code'],'duplicate')
        with CommandLedger(self.path) as ledger:
            self.assertEqual(ledger.get('close-00000001')['state'],'outcome_unknown')

if __name__=='__main__': unittest.main()
