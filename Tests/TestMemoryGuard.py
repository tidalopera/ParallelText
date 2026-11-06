from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from MemoryGuard import MemoryGuard, LOW_MEMORY_BYTES


class GuardTests(unittest.TestCase):
    def fixture(self, directory, free):
        pid=Path(directory)/"Model.pid";pid.write_text("123",encoding="utf-8")
        memory={"free":free,"resident":1_000_000_000,"clock":0}
        actions=[]
        def execute(script):
            actions.append(script)
            if script=="stop":
                memory["free"]+=memory["resident"];memory["resident"]=0
            else:
                memory["resident"]=1_000_000_000;memory["free"]-=memory["resident"]
        guard=MemoryGuard({"furinaStartScript":"start","furinaStopScript":"stop","furinaPidFile":str(pid)},
            threading.Event(),Path(directory)/"State.json",available=lambda:memory["free"],
            resident=lambda _:memory["resident"],execute=execute,health=lambda:{"ready":True},clock=lambda:memory["clock"])
        return guard,memory,actions

    def test_half_gb_boundary_continues(self):
        with TemporaryDirectory() as directory:
            guard,memory,actions=self.fixture(directory,LOW_MEMORY_BYTES)
            guard.tick()
            self.assertEqual(actions,[])
            self.assertFalse(guard.waiting())

    def test_below_threshold_releases_and_does_not_thrash(self):
        with TemporaryDirectory() as directory:
            guard,memory,actions=self.fixture(directory,LOW_MEMORY_BYTES-1)
            guard.tick()
            self.assertEqual(actions,["stop"])
            self.assertEqual(memory["resident"],0)
            self.assertTrue(guard.status["released"])
            memory["clock"]=60
            guard.tick()
            self.assertEqual(actions,["stop"],"Do not reload solely because unloading freed memory")
            memory["free"]=2_000_000_000
            guard.tick()
            self.assertEqual(actions,["stop","start"])
            self.assertFalse(guard.waiting())

    def test_released_state_survives_reader_restart(self):
        with TemporaryDirectory() as directory:
            guard,memory,actions=self.fixture(directory,100_000_000)
            guard.tick()
            restarted,_,_=self.fixture(directory,1_000_000_000)
            self.assertTrue(restarted.status["released"])
            self.assertGreater(restarted.status["reloadRequiredBytes"],LOW_MEMORY_BYTES)


if __name__=="__main__":unittest.main()
