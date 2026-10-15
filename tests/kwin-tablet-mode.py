#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the real KWin switch spies through deferred deletion and mixed switches."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
PROJECT=Path(__file__).resolve().parents[1]
def source(work, archive):
 record=json.loads((PROJECT/'device/kwin/source.json').read_text())
 if hashlib.sha256(archive.read_bytes()).hexdigest()!=record['sha256']:raise ValueError('KWin 源码校验失败。')
 with tarfile.open(archive) as tar:tar.extractall(work,filter='data')
 upstream=work/('kwin-'+record['version'])
 subprocess.run(['patch','--batch','--forward','--fuzz=0','-p1','-i',str(PROJECT/'device/kwin/0001-tablet-switch-state.patch')],cwd=upstream,check=True)
 return upstream,record
p=argparse.ArgumentParser();p.add_argument('--source-archive',type=Path);p.add_argument('--unpatched',action='store_true');a=p.parse_args()
archive=a.source_archive or PROJECT/'inputs/kwin/kwin-6.7.5.tar.xz'
if not archive.is_file():raise SystemExit('需要单独下载 KWin 研究源码，或 --source-archive 指定归档。')
with tempfile.TemporaryDirectory() as directory:
 work=Path(directory)
 if a.unpatched:
  with tarfile.open(archive) as tar:tar.extractall(work,filter='data')
  upstream=work/'kwin-6.7.5'
 else:upstream,_=source(work,archive)
 text=(upstream/'src/tabletmodemanager.cpp').read_text()
 spies=text[text.index('static bool blocksTabletMode'):text.index('TabletModeManager::TabletModeManager()')]
 lidtext=(upstream/'src/lidswitchtracker.cpp').read_text()
 lidbody=lidtext[lidtext.index('void LidSwitchTracker::switchEvent'):lidtext.index('\n}\n\n#include')]
 prefix=r'''
#include <QObject>
#include <QCoreApplication>
#include <QVector>
#include <algorithm>
#include <iostream>
namespace KWin {
enum class SwitchType { Lid, TabletMode };
enum class SwitchState { Off, On };
class InputDevice : public QObject {
 Q_OBJECT
public:
 bool touch=false,pointer=false;
 bool isTouch()const{return touch;}
 bool isPointer()const{return pointer;}
 bool isTabletModeSwitch()const{return true;}
 bool isLidSwitch()const{return true;}
};
struct udev_device{};
inline udev_device *libinput_device_get_udev_device(void*){static udev_device u;return &u;}
inline bool udev_device_has_tag(udev_device*,const char*){return false;}
inline void udev_device_unref(udev_device*){}
namespace LibInput {
class Device : public InputDevice {
 Q_OBJECT
public:void *device(){return nullptr;}
};
}
struct SwitchEvent { SwitchType type; InputDevice *device; SwitchState state; };
class InputEventSpy {public:virtual ~InputEventSpy()=default;virtual void switchEvent(SwitchEvent*){}};
class InputRedirection : public QObject {
 Q_OBJECT
public:bool hasSwitch=false;QVector<InputDevice*> list;
 auto devices()const{return list;}
 bool hasTabletModeSwitch()const{return hasSwitch;}
 void add(InputDevice*d){list.append(d);Q_EMIT deviceAdded(d);}
Q_SIGNALS:
 void deviceAdded(InputDevice*);
 void deviceRemoved(InputDevice*);
 void hasTabletModeSwitchChanged(bool);
};
InputRedirection *input(){static InputRedirection i;return &i;}
class TabletModeManager : public QObject {
public:bool tablet=false,available=false;
 void setIsTablet(bool b){tablet=b;}
 void setTabletModeAvailable(bool b){available=b;}
};
class LidSwitchTracker : public QObject {
 Q_OBJECT
public:bool m_isLidClosed=false;
 void switchEvent(SwitchEvent*);
Q_SIGNALS:void lidStateChanged();
};
'''
 suffix=r'''
}
int main(int argc,char**argv){
 QCoreApplication app(argc,argv);using namespace KWin;
 int failures=0;
 auto check=[&](bool ok,const char*name){std::cout<<(ok?"PASS ":"FAIL ")<<name<<"\n";if(!ok)failures++;};
 TabletModeManager manager;
 auto *fallback=new TabletModeTouchpadRemovedSpy(&manager);
 QObject::connect(input(),&InputRedirection::hasTabletModeSwitchChanged,fallback,[fallback](bool b){if(b)fallback->deleteLater();});
 LibInput::Device touch,pointer,latePointer,switches;
 touch.touch=true;pointer.pointer=true;latePointer.pointer=true;
 input()->add(&touch);input()->add(&pointer);
 check(!manager.tablet,"fallback detects mouse without a switch");
 input()->hasSwitch=true;Q_EMIT input()->hasTabletModeSwitchChanged(true);
 TabletModeSwitchEventSpy spy(&manager);
 SwitchEvent tabletOn{SwitchType::TabletMode,&switches,SwitchState::On};
 spy.switchEvent(&tabletOn);
 input()->add(&latePointer);
 check(manager.tablet,"real switch survives pointer enumeration before deleteLater");
 manager.tablet=true;
 SwitchEvent lidOff{SwitchType::Lid,&switches,SwitchState::Off};spy.switchEvent(&lidOff);
 check(manager.tablet,"lid event on combined device does not turn tablet mode off");
 SwitchEvent tabletOff{SwitchType::TabletMode,&switches,SwitchState::Off};spy.switchEvent(&tabletOff);
 check(!manager.tablet,"tablet switch can leave tablet mode");
 LidSwitchTracker lid;lid.switchEvent(&tabletOn);
 check(!lid.m_isLidClosed,"tablet event does not close lid on combined device");
 SwitchEvent lidOn{SwitchType::Lid,&switches,SwitchState::On};lid.switchEvent(&lidOn);
 check(lid.m_isLidClosed,"real lid event still works");
 QCoreApplication::sendPostedEvents(nullptr,QEvent::DeferredDelete);
 return failures?1:0;
}
#include "regression.moc"
'''
 cpp=work/'regression.cpp';cpp.write_text(prefix+spies+lidbody+suffix)
 flags=shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs','Qt6Core'],text=True))
 libexec=subprocess.check_output(['pkg-config','--variable=libexecdir','Qt6Core'],text=True).strip()
 moc=shutil.which('moc6') or shutil.which('moc') or str(Path(libexec)/'moc')
 subprocess.run([moc,str(cpp),'-o',str(work/'regression.moc')],check=True)
 subprocess.run(['c++','-std=c++20','-fPIC',str(cpp),'-o',str(work/'regression'),*flags],check=True)
 subprocess.run([str(work/'regression')],check=True)
