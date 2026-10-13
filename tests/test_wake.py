"""Wake-word state machine and transcript handling (wispr/wake.py).

No audio, no models: the Machine is pure, so every path is driven by hand.
"""
import unittest

from wispr import wake
from wispr.wake import Machine, WakeConfig, is_pause, is_resume, sound_key, strip_wake


def cfg(**kw):
    c = WakeConfig(enabled=True, phrases=["hey coop", "hey koop"], followup_s=18.0,
                   endpoint_s=0.9, max_s=30.0)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def kinds(acts):
    return [a[0] for a in acts]


def run_capture(m, t0=100.0, speech_until=102.0):
    """Wake at t0, speech until speech_until, then silence; returns end tick time."""
    assert kinds(m.keyword("wake", t0)) == ["listen", "start"]
    t = t0
    while t < speech_until:
        t = round(t + 0.1, 2)
        m.vad(True, t)
        assert m.tick(t) == []
    while True:
        t = round(t + 0.1, 2)
        m.vad(False, t)
        acts = m.tick(t)
        if acts:
            return t, acts


class TestTranscript(unittest.TestCase):
    def test_sound_key(self):
        self.assertEqual(sound_key("Coop"), sound_key("koop"))
        self.assertEqual(sound_key("coup"), sound_key("co-op"))
        self.assertNotEqual(sound_key("Cooper"), sound_key("coop"))
        self.assertNotEqual(sound_key("cool"), sound_key("coop"))
        self.assertNotEqual(sound_key("cop"), sound_key("cope") + "x")

    def test_strip_keeps_request(self):
        c = cfg()
        self.assertEqual(strip_wake("Hey Coop, what's next?", c), (True, "What's next?"))
        self.assertEqual(strip_wake("Hey, Koop. Add milk to my list.", c), (True, "Add milk to my list."))
        self.assertEqual(strip_wake("Okay hey coop read my to-dos", c), (True, "Read my to-dos"))
        self.assertEqual(strip_wake("Coop what time is it", c), (True, "What time is it"))
        self.assertEqual(strip_wake("Hey Coop.", c), (True, ""))

    def test_strip_rejects_lookalikes(self):
        c = cfg()
        for s in ["Hey Cooper, how are you?", "Hey, cool, let's go.", "Hey, could you pass the salt?",
                  "The cop stopped the car.", "I bought a coupe", "What time is it?"]:
            self.assertFalse(strip_wake(s, c)[0], s)

    def test_aliases(self):
        c = cfg(aliases=["kupe"])
        self.assertTrue(strip_wake("Hey kupe, hi", c)[0])

    def test_pause_resume_phrases(self):
        c = cfg()
        self.assertTrue(is_pause("Coop, stop listening.", c))
        self.assertTrue(is_pause("Stop listening", c))
        self.assertFalse(is_pause("Stop listening to that podcast and play the next one", c))
        self.assertTrue(is_resume("Coop, start listening", c))
        self.assertEqual(c.resume_phrase, "coop start listening")

    def test_config_defaults_off(self):
        self.assertFalse(WakeConfig().enabled)

    def test_thresholds_strict_is_harder(self):
        c = cfg(sensitivity=0.6)
        b, t = c.thresholds()
        bs, ts = c.thresholds(strict=True)
        self.assertGreater(ts, t)
        self.assertLess(bs, b)


class TestMachine(unittest.TestCase):
    def test_full_cycle_with_followup(self):
        m = Machine(cfg())
        t, acts = run_capture(m)
        self.assertEqual(acts, [("end", "wake")])
        self.assertAlmostEqual(t, 102.9, places=1)    # 0.9 s endpoint
        self.assertEqual(m.state, "busy")
        m.heard(True, t)
        self.assertEqual(m.state, "answering")
        m.speaking_note(t + 1)
        self.assertFalse(m.wants_vad())              # echo gate: Coop is talking
        self.assertEqual(m.vad(True, t + 2), [])
        m.spoke(t + 5)
        self.assertEqual(m.state, "followup")
        # echo tail: speech right after Coop stops is ignored
        self.assertEqual(m.vad(True, t + 5.2), [])
        acts = m.vad(True, t + 6)
        self.assertEqual(kinds(acts), ["listen", "start"])
        self.assertEqual(acts[1][1], "followup")
        self.assertEqual(m.state, "capture")

    def test_followup_window_closes_quietly(self):
        m = Machine(cfg())
        t, _ = run_capture(m)
        m.heard(True, t)
        m.speaking_note(t)
        m.spoke(t + 3)
        self.assertEqual(m.tick(t + 3 + 17.9), [])
        self.assertEqual(m.state, "followup")
        self.assertEqual(m.tick(t + 3 + 18.1), [])
        self.assertEqual(m.state, "idle")
        self.assertEqual(m.vad(True, t + 30), [])    # idle: VAD never starts a take

    def test_speech_in_same_breath_is_kept(self):
        m = Machine(cfg())
        m.keyword("wake", 10.0)
        for i in range(1, 15):                        # talking straight through
            m.vad(True, 10 + i * 0.1)
        self.assertTrue(m.speech_seen)

    def test_bare_wake_waits_then_ends(self):
        m = Machine(cfg())
        m.keyword("wake", 10.0)
        self.assertEqual(m.tick(14.0), [])            # pausing after "hey coop" is fine
        self.assertEqual(m.tick(15.1), [("end", "wake")])

    def test_hard_cap(self):
        m = Machine(cfg(max_s=30.0))
        m.keyword("wake", 0.0)
        for i in range(1, 301):
            m.vad(True, i * 0.1)
            acts = m.tick(i * 0.1)
            if acts:
                break
        self.assertEqual(acts, [("end", "wake")])
        self.assertGreaterEqual(i * 0.1, 29.9)

    def test_rejected_transcript_cancels(self):
        m = Machine(cfg())
        t, _ = run_capture(m)
        self.assertEqual(m.heard(False, t), [("cancel",)])
        self.assertEqual(m.state, "idle")

    def test_no_second_wake_while_capturing_or_busy(self):
        m = Machine(cfg())
        m.keyword("wake", 0.0)
        self.assertEqual(m.keyword("wake", 0.5), [])
        m.state = "busy"
        self.assertEqual(m.keyword("wake", 1.0), [])

    def test_barge_in_while_coop_speaks(self):
        m = Machine(cfg())
        m.speaking_note(0.0)
        self.assertTrue(m.strict())
        acts = m.keyword("wake", 1.0)
        self.assertEqual(kinds(acts), ["listen", "start"])
        self.assertFalse(m.speaking)

    def test_held_key_mutes_and_drops_capture(self):
        m = Machine(cfg())
        m.keyword("wake", 0.0)
        self.assertEqual(m.set_held(True, 0.5), [("abort",)])
        self.assertEqual(m.state, "idle")
        self.assertEqual(m.keyword("wake", 1.0), [])
        self.assertFalse(m.wants_vad())
        m.set_held(False, 2.0)
        self.assertEqual(kinds(m.keyword("wake", 3.0)), ["listen", "start"])

    def test_dictation_closes_followup(self):
        m = Machine(cfg())
        m.speaking_note(0)
        m.spoke(1.0)
        self.assertEqual(m.state, "followup")
        m.set_held(True, 2.0)
        self.assertEqual(m.state, "idle")

    def test_call_mutes(self):
        m = Machine(cfg())
        m.keyword("wake", 0.0)
        self.assertEqual(m.set_call(True, 0.5), [("abort",), ("cancel",)])
        self.assertEqual(m.keyword("wake", 1.0), [])
        m.speaking_note(2.0)
        m.spoke(3.0)
        self.assertEqual(m.state, "idle")             # no follow-up during a call
        m.set_call(False, 4.0)
        self.assertEqual(kinds(m.keyword("wake", 5.0)), ["listen", "start"])

    def test_pause_and_resume_by_phrase(self):
        m = Machine(cfg())
        acts = m.pause(0.0)
        self.assertEqual(kinds(acts), ["say"])
        self.assertIn("start listening", acts[0][1])
        self.assertEqual(m.keyword("wake", 1.0), [])
        m.speaking_note(2.0)
        m.spoke(3.0)
        self.assertEqual(m.state, "idle")             # paused: no follow-up
        self.assertEqual(kinds(m.keyword("resume", 4.0)), ["say"])
        self.assertFalse(m.paused)
        self.assertEqual(kinds(m.keyword("wake", 5.0)), ["listen", "start"])

    def test_resume_ignored_when_not_paused(self):
        m = Machine(cfg())
        self.assertEqual(m.keyword("resume", 0.0), [])

    def test_coop_key_resumes_and_takes_over(self):
        m = Machine(cfg())
        m.pause(0.0)
        m.coop_key(1.0)
        self.assertFalse(m.paused)
        m.keyword("wake", 2.0)
        self.assertEqual(m.coop_key(2.5), [("abort",)])
        self.assertEqual(m.state, "idle")

    def test_fallback_when_overlay_sends_no_notes(self):
        m = Machine(cfg())
        t, _ = run_capture(m)
        m.heard(True, t)
        self.assertEqual(m.tick(t + 5), [])
        self.assertEqual(m.state, "answering")
        m.tick(t + wake.FALLBACK_SPOKE_S + 0.1)
        self.assertEqual(m.state, "followup")

    def test_spoke_without_speaking_does_not_open_window(self):
        m = Machine(cfg())
        m.spoke(1.0)                                  # a bare cancel() from the overlay
        self.assertEqual(m.state, "idle")


class TestIntercept(unittest.TestCase):
    def test_intercept_without_listener_is_noop(self):
        wake._LISTENER = None
        self.assertFalse(wake.intercept_hear("Coop, stop listening"))


if __name__ == "__main__":
    unittest.main()
