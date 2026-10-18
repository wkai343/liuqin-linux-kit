#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the actual unprivileged recovery policy without contacting a device."""
from pathlib import Path
import importlib.machinery,importlib.util,json,subprocess,tempfile,unittest
from unittest.mock import patch

P=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('display_recovery',str(P/'device/gnome-overlay/usr/local/libexec/liuqin-display-recovery'))
spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)


def state(**changes):
    s=dict(version=1,available=1,enabled=1,pending=0,armed=1,cycle=1,events=0,last=0,age_ms=5000,brightness=510,max_brightness=4095,bl_power=0,raw_brightness=1500,pwm_level=2047)
    s.update(changes);return s


class FakeSession:
    def __init__(self,before,*,fail_off=False,fail_on=False,unchanged=False,active=True):
        self.before=before;self.calls=[];self.fail_off=fail_off;self.fail_on=fail_on;self.unchanged=unchanged;self.active=active
    def still_active(self):return self.active
    def restore_allowed(self):return True
    def power(self,mode):
        self.calls.append(mode)
        if mode=='off' and self.fail_off or mode=='on' and self.fail_on:raise OSError('command failure')
    def read(self):
        if self.calls[-1]=='off':return state(enabled=0,bl_power=4,raw_brightness=0,pwm_level=0)
        return self.before if self.unchanged else state(cycle=self.before['cycle']+1)
    def wait(self,seconds):pass


class Tests(unittest.TestCase):
    def test_healthy_state_never_recovers(self):
        p=m.Policy()
        for t in range(100):self.assertIsNone(p.reason(state(),t))
    def test_startup_blank_zero_request_and_unavailable_do_not_recover(self):
        for changes in [dict(available=0),dict(enabled=0),dict(cycle=0),dict(age_ms=2999),dict(bl_power=4),dict(raw_brightness=0)]:
            with self.subTest(changes=changes):
                p=m.Policy();s=state(pwm_level=0,pending=1,events=1,**changes)
                for t in [0,5,50]:self.assertIsNone(p.reason(s,t))
    def test_irq_and_privileged_injection(self):
        p=m.Policy();self.assertEqual(p.reason(state(pending=1,events=1),10),'panel-irq')
        self.assertEqual(p.reason(state(pending=1,events=1,last=2),10),'software-test')
    def test_zero_pwm_requires_four_seconds(self):
        p=m.Policy();s=state(pwm_level=0)
        self.assertIsNone(p.reason(s,10));self.assertIsNone(p.reason(s,13.9));self.assertEqual(p.reason(s,14),'persistent-zero-pwm')
    def test_adaptive_pwm_does_not_create_a_false_fault(self):
        p=m.Policy();s=state(pwm_level=0,pwm_fault_eligible=False,pwm_content_adaptive=True)
        for t in [0,4,60,600]:self.assertIsNone(p.reason(s,t))
        self.assertEqual(p.reason(dict(s,pending=1,events=1),601),'panel-irq')
        self.assertEqual(p.reason(dict(s,pending=1,events=1,last=2),601),'software-test')
    def test_cabc_switch_clears_old_pwm_timer(self):
        p=m.Policy();p.reason(state(pwm_level=0),10)
        self.assertIsNone(p.reason(state(pwm_level=0,pwm_fault_eligible=False),15))
        self.assertIsNone(p.reason(state(pwm_level=0),16))
        self.assertEqual(p.reason(state(pwm_level=0),20),'persistent-zero-pwm')
    def test_cabc_mode_and_generation_are_validated(self):
        with tempfile.TemporaryDirectory() as t:
            node=Path(t);panel=node/'esd_status';status=node/'cabc_status'
            self.assertEqual(m.cabc_pwm_policy(panel,3),dict(pwm_fault_eligible=True,pwm_content_adaptive=False,cabc_mode='legacy'))
            status.write_text('requested=unchanged programmed=unknown cycle=0 ready=1 error=0')
            self.assertTrue(m.cabc_pwm_policy(panel,3)['pwm_fault_eligible'])
            for mode in ['off','ui','still','movie']:
                status.write_text(f'requested={mode} programmed={mode} cycle=3 ready=1 error=0')
                result=m.cabc_pwm_policy(panel,3)
                self.assertEqual(result['pwm_fault_eligible'],mode=='off')
                self.assertEqual(result['pwm_content_adaptive'],mode!='off')
                self.assertEqual(result['cabc_mode'],mode)
                self.assertFalse(m.cabc_pwm_policy(panel,4)['pwm_content_adaptive'])
    def test_unknown_cabc_state_does_not_mask_gpio_faults(self):
        with tempfile.TemporaryDirectory() as t:
            node=Path(t);panel=node/'esd_status';status=node/'cabc_status'
            good='requested=ui programmed=ui cycle=3 ready=1 error=0'
            for bad in ['',good+' ready=1',good+' unexpected=0',good.replace('error=0','error=-5'),good.replace('ready=1','ready=0'),good.replace('programmed=ui','programmed=unknown'),good.replace('cycle=3','cycle=x'),good.replace('ui','invalid'),'nonsense']:
                with self.subTest(bad=bad):
                    status.write_text(bad);policy=m.cabc_pwm_policy(panel,3)
                    self.assertFalse(policy['pwm_fault_eligible']);self.assertFalse(policy['pwm_content_adaptive'])
                    p=m.Policy();s=state(cycle=3,pwm_level=0,**policy)
                    self.assertIsNone(p.reason(s,1));self.assertIsNone(p.reason(s,20))
                    self.assertEqual(p.reason(dict(s,pending=1,events=1),21),'panel-irq')
    def test_cabc_read_failure_is_not_legacy_mode(self):
        with patch.object(Path,'read_text',side_effect=OSError('read failed')):
            result=m.cabc_pwm_policy(Path('/sys/panel/esd_status'),3)
        self.assertFalse(result['pwm_fault_eligible']);self.assertFalse(result['pwm_content_adaptive'])
        with patch.object(Path,'read_text',side_effect=UnicodeError('invalid encoding')):
            self.assertFalse(m.cabc_pwm_policy(Path('/sys/panel/esd_status'),3)['pwm_fault_eligible'])
    def test_adaptive_black_frame_can_complete_recovery(self):
        before=state(pending=1,events=1);s=FakeSession(before)
        original=s.read
        def read():
            result=original()
            if s.calls[-1]=='on':result.update(pwm_level=0,pwm_fault_eligible=False,pwm_content_adaptive=True)
            return result
        s.read=read;after=m.recover(s,before)
        self.assertEqual(after['cycle'],2);self.assertEqual(after['pwm_level'],0);self.assertEqual(s.calls,['off','on'])
    def test_unknown_mode_with_zero_pwm_does_not_claim_recovery(self):
        s=FakeSession(state());original=s.read
        def read():
            result=original()
            if s.calls[-1]=='on':result.update(pwm_level=0,pwm_fault_eligible=False,pwm_content_adaptive=False)
            return result
        s.read=read
        with self.assertRaisesRegex(RuntimeError,'did not recover'):m.recover(s,state())
    def test_adaptive_mode_still_requires_a_real_new_frame(self):
        for changes in [dict(enabled=0),dict(cycle=1),dict(bl_power=4),dict(raw_brightness=0)]:
            with self.subTest(changes=changes):
                s=state(cycle=2,pwm_level=0,pwm_content_adaptive=True);s.update(changes)
                self.assertFalse(m.completed_frame(s,1))
    def test_transient_pwm_resets_timer(self):
        p=m.Policy();p.reason(state(pwm_level=0),10);p.reason(state(),13)
        self.assertIsNone(p.reason(state(pwm_level=0),14));self.assertEqual(p.reason(state(pwm_level=0),18),'persistent-zero-pwm')
    def test_new_generation_resets_timer(self):
        p=m.Policy();p.reason(state(pwm_level=0),10)
        self.assertIsNone(p.reason(state(pwm_level=0,cycle=2),20))
    def test_duplicate_irq_and_sixty_second_cooldown(self):
        p=m.Policy();s=state(pending=1,events=1);p.reserve(s,100)
        self.assertIsNone(p.reason(s,161));self.assertIsNone(p.reason(state(pending=1,events=2),159))
        self.assertEqual(p.reason(state(pending=1,events=2),160),'panel-irq')
    def test_two_attempt_cap_survives_restart_and_expires(self):
        p=m.Policy();p.reserve(state(events=1),10);p.reserve(state(events=2),80);p=m.Policy(p.data())
        self.assertIsNone(p.reason(state(pending=1,events=3),609))
        self.assertEqual(p.reason(state(pending=1,events=3),611),'panel-irq')
    def test_display_cycle_and_readback(self):
        before=state(pending=1,events=1,pwm_level=0);s=FakeSession(before)
        after=m.recover(s,before);self.assertEqual(s.calls,['off','on']);self.assertEqual(after['cycle'],2);self.assertGreater(after['pwm_level'],0)
    def test_off_failure_still_restores_on(self):
        s=FakeSession(state(),fail_off=True)
        with self.assertRaises(OSError):m.recover(s,state())
        self.assertEqual(s.calls,['off','on'])
    def test_delayed_off_commit_is_polled(self):
        s=FakeSession(state());original_read=s.read;off_reads=[]
        def read():
            if s.calls[-1]=='off':
                off_reads.append(1)
                if len(off_reads)<3:return state()
            return original_read()
        s.read=read
        self.assertEqual(m.recover(s,state())['cycle'],2)
        self.assertEqual(len(off_reads),3);self.assertEqual(s.calls,['off','on'])
    def test_missed_blank_sample_accepts_a_completed_new_frame(self):
        s=FakeSession(state());s.read=lambda:state(cycle=2)
        self.assertEqual(m.recover(s,state())['cycle'],2)
        self.assertEqual(s.calls,['off','on'])
    def test_off_never_commits_is_bounded_and_restores_on(self):
        s=FakeSession(state());reads=[]
        def read():reads.append(1);return state()
        s.read=read
        with self.assertRaisesRegex(RuntimeError,'did not power off'):m.recover(s,state())
        self.assertEqual(len(reads),6);self.assertEqual(s.calls,['off','on'])
    def test_new_frame_with_zero_pwm_does_not_confirm_off(self):
        s=FakeSession(state());s.read=lambda:state(cycle=2,pwm_level=0)
        with self.assertRaisesRegex(RuntimeError,'did not power off'):m.recover(s,state())
        self.assertEqual(s.calls,['off','on'])
    def test_on_failure_is_not_claimed_success(self):
        s=FakeSession(state(),fail_on=True)
        with self.assertRaises(OSError):m.recover(s,state())
        self.assertEqual(s.calls,['off','on'])
    def test_no_new_frame_is_failure(self):
        s=FakeSession(state(),unchanged=True)
        with self.assertRaisesRegex(RuntimeError,'did not recover'):m.recover(s,state())
    def test_changed_session_has_no_side_effect(self):
        s=FakeSession(state(),active=False)
        with self.assertRaises(ValueError):m.recover(s,state())
        self.assertEqual(s.calls,[])
    def test_external_outputs_excluded(self):
        self.assertEqual(m.excluded_outputs({'outputs':[{'name':'DSI-1','connected':True,'enabled':True},{'name':'DP-1','connected':True,'enabled':True},{'name':'HDMI-A-1','connected':False,'enabled':False}]}),['DP-1','HDMI-A-1'])
    def test_invalid_or_inactive_internal_output_refused(self):
        for outputs in [[],[{'name':'DSI-1','enabled':False,'connected':True}],[{'name':'DSI-2','enabled':True,'connected':True}],[{'name':'DSI-1','enabled':True,'connected':True}]*2,[{'name':'bad;command'}]]:
            with self.subTest(outputs=outputs),self.assertRaises(ValueError):m.excluded_outputs({'outputs':outputs})
    def test_status_format(self):
        s={k:v for k,v in state().items() if k in ['version','available','enabled','pending','armed','cycle','events','last','age_ms']}
        text=' '.join(f'{k}={v}' for k,v in s.items());self.assertEqual(m.parse_status(text),s)
        for bad in [text+' version=1',text.replace('version=1','version=2'),text.replace('armed=1','armed=4'),text.replace('events=0','events=-1'),text+' extra=3']:
            with self.subTest(bad=bad),self.assertRaises(ValueError):m.parse_status(bad)
    def test_read_errors_never_become_pwm_zero(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);p=root/'esd_status';p.write_text(' '.join(f'{k}={v}' for k,v in state().items() if k in ['version','available','enabled','pending','armed','cycle','events','last','age_ms']))
            for k in ['brightness','max_brightness','bl_power','raw_brightness']:(root/k).write_text(str(state()[k]))
            with patch.object(m,'BACKLIGHT',root),self.assertRaises(OSError):m.snapshot(p)
    def test_non_active_foreign_or_remote_session_refused(self):
        good={'User':'1000','Type':'wayland','Active':'yes','Remote':'no','Seat':'seat0'}
        for change in [dict(User='989'),dict(Type='x11'),dict(Active='no'),dict(Remote='yes'),dict(Seat='seat1')]:
            props=dict(good,**change)
            with self.subTest(change=change),patch.object(m.subprocess,'run',return_value=type('R',(),{'stdout':'running\n'})()),patch.object(m,'command',side_effect=['7','\n'.join(f'{k}={v}' for k,v in props.items())]),self.assertRaises(ValueError):m.active_session(1000)
    def test_starting_system_still_requires_the_active_wayland_user(self):
        with patch.object(m.subprocess,'run',return_value=type('R',(),{'stdout':'starting\n'})()),patch.object(m,'command',side_effect=['7','User=989\nType=wayland\nActive=yes\nRemote=no\nSeat=seat0']),self.assertRaisesRegex(m.Deferred,'this user does not own') as err:
            m.active_session(1000)
        self.assertEqual(err.exception.delay,10)
    def test_missing_session_has_short_readiness_retry(self):
        with patch.object(m.subprocess,'run',return_value=type('R',(),{'stdout':'starting\n'})()),patch.object(m,'command',return_value=''),self.assertRaises(m.Deferred) as err:
            m.active_session(1000)
        self.assertEqual(err.exception.delay,2)
    def test_compositor_not_started_has_short_readiness_retry(self):
        props='User=1000\nType=wayland\nActive=yes\nRemote=no\nSeat=seat0'
        with patch.object(m.subprocess,'run',return_value=type('R',(),{'stdout':'starting\n'})()),patch.object(m,'command',side_effect=['7',props,subprocess.CalledProcessError(1,['pgrep'])]),self.assertRaises(m.Deferred) as err:
            m.active_session(1000)
        self.assertEqual(err.exception.delay,2)
    def test_shutdown_is_not_recovery_time(self):
        with patch.object(m.subprocess,'run',return_value=type('R',(),{'stdout':'stopping\n'})()),self.assertRaises(ValueError):m.active_session(1000)
    def test_shutdown_after_off_is_not_reenabled(self):
        s=FakeSession(state())
        s.restore_allowed=lambda:False
        with self.assertRaisesRegex(RuntimeError,'system is stopping'):m.recover(s,state())
        self.assertEqual(s.calls,['off'])
    def test_corrupt_policy_state_is_ignored(self):
        for saved in [[],None,{'attempts':None,'handled':'broken'},{'attempts':[float('nan'),-1,{}],'handled':-2}]:
            p=m.Policy(saved);self.assertEqual(p.attempts,[]);self.assertEqual(p.handled,0)
    def test_root_refused(self):
        with patch.object(m.os,'geteuid',return_value=0),self.assertRaises(ValueError):m.run(True)


if __name__=='__main__':unittest.main(verbosity=2)
