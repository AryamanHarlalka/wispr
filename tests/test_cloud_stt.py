"""Cloud STT guards: never paste a prompt echo or invented text, never
boost a loud take, always lift a quiet one, never raise."""
import numpy as np
from wispr import cloud_stt as c

P = c.build_prompt(["Priya", "Northwind", "Zephyr", "Okafor", "Quillon",
                    "Brightline", "Marlow", "Tessera", "Juniper"], "")

# plausibility
assert c.plausible("Ping Priya about the Northwind standup at 3.", P, 6.0)
assert not c.plausible("", P, 3.0)
assert not c.plausible("Names and terms: Priya, Northwind, Zephyr.", P, 3.0)
assert not c.plausible("Priya, Northwind, Zephyr, Okafor, Quillon, Brightline, Marlow, "
                       "Tessera, Juniper and more", P, 4.0)
assert not c.plausible(" ".join(["word"] * 60), P, 2.0)   # 60 words in 2 s

# normalisation
loud = (np.sin(np.linspace(0, 400, 16000)) * 0.9).astype(np.float32)
assert np.allclose(c.normalize(loud), loud - loud.mean(), atol=1e-6)
quiet = loud * 0.05
q = c.normalize(quiet)
assert 0.6 < float(np.max(np.abs(q))) <= 0.71, np.max(np.abs(q))
hiss = loud * 0.001
assert float(np.max(np.abs(c.normalize(hiss)))) < 0.02   # gain cap holds
assert c.normalize(np.zeros(0, np.float32)).size == 0

# transcribe never raises, even with no key / tiny audio
c._key = None
import os; os.environ.pop("OPENAI_API_KEY", None)
text, why = c.transcribe(np.zeros(100, np.float32), P)
assert text is None
print("test_cloud_stt: all passed")
