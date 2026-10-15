#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the patched upstream completion flow, with isolated service doubles."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'build/lib'))
from plasma_setup import source


def method(text,name):
    start=text.index(name);brace=text.index('{',start);depth=0
    for offset in range(brace,len(text)):
        if text[offset]=='{':depth+=1
        elif text[offset]=='}':
            depth-=1
            if depth==0:return text[start:offset+1]
    raise AssertionError('unterminated source method')


parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--distribution',choices=('ubuntu',),default='ubuntu')
parser.add_argument('--source-archive',type=Path)
args=parser.parse_args()
lock='plasma-setup-source.json'
record=json.loads((PROJECT/'build/rootfs'/lock).read_text())
if args.source_archive is None:args.source_archive=PROJECT/'inputs/plasma-setup'/record['archive']
if not args.source_archive.is_file():
    print('SKIP: run prepare first to obtain the pinned Plasma Setup source');sys.exit(0)
if not shutil.which('c++'):
    print('SKIP: C++ compiler required');sys.exit(0)
with tempfile.TemporaryDirectory() as directory:
    work=Path(directory);upstream,_=source(work,args.source_archive,record)
    patches='plasma-setup'
    for patch in sorted((PROJECT/'build/rootfs'/patches).glob('*.patch')):
        subprocess.run(['patch','--batch','--forward','-p1','-i',str(patch)],cwd=upstream,check=True,stdout=subprocess.DEVNULL)
    text=(upstream/'src/initialstartutil.cpp').read_text()
    signature='void InitialStartUtil::finish()'
    methods='\n'.join(method(text,name) for name in (signature,
        'bool InitialStartUtil::doUserCreationSteps()','bool InitialStartUtil::createCompletionFlag()'))
    harness=r'''
#include <cassert>
#include <iostream>
#include <string>
#define Q_EMIT
#define QStringLiteral(x) x
std::string i18n(const char *message) { return message; }
struct Logger { template<typename T> Logger &operator<<(T const &) { return *this; } };
#define qCInfo(x) Logger()
#define qCWarning(x) Logger()
struct Account { bool exists=false, succeeds=true; int creates=0;
 bool hasExistingUsers(){ return exists; }
 bool createUser(){++creates;return succeeds;}
 std::string username(){return "test";}
};
struct DisplayUtil {
 void setGlobalThemeForNewUser(void *,const std::string &){}
 void setScalingForNewUser(void *,const std::string &){}
};
bool flagSucceeds=true;int flagRequests=0;
namespace KAuth {
 struct ExecuteJob { bool exec(){++flagRequests;return flagSucceeds;} std::string errorString(){return "fixture failure";} };
 struct Action { Action(const char *){} void setParentWindow(void *){} void setHelperId(const char *){}
  ExecuteJob *execute(){static ExecuteJob job;return &job;} };
}
struct InitialStartUtil {
 Account *m_accountController;void *m_window=nullptr;std::string m_errorMessage;
 bool m_newUserCreated=false;int logouts=0;
 explicit InitialStartUtil(Account &account):m_accountController(&account){}
 void errorMessageChanged(){}
 void finish();bool doUserCreationSteps();bool createCompletionFlag();
 void logOut(){++logouts;}
};
'''+methods+r'''
int main() {
 Account failing;failing.succeeds=false;InitialStartUtil first(failing);
 first.finish();assert(flagRequests==0 && first.logouts==0 && !first.m_errorMessage.empty());
 failing.succeeds=true;first.finish();assert(flagRequests==1 && first.logouts==1 && first.m_errorMessage.empty());
 Account retry;InitialStartUtil second(retry);flagRequests=0;flagSucceeds=false;
 second.finish();assert(retry.creates==1 && flagRequests==1 && second.logouts==0 && !second.m_errorMessage.empty());
 flagSucceeds=true;second.finish();assert(retry.creates==1 && flagRequests==2 && second.logouts==1 && second.m_errorMessage.empty());
 Account existing;existing.exists=true;InitialStartUtil third(existing);third.finish();
 assert(existing.creates==0 && third.logouts==1);
 std::cout << "PASS: patched upstream completion rejects account failures, keeps failed flag writes retryable, reuses created accounts, and handles existing users\n";
}
'''
    cpp=work/'completion.cpp';cpp.write_text(harness)
    subprocess.run(['c++','-std=c++17',str(cpp),'-o',str(work/'completion')],check=True)
    subprocess.run([str(work/'completion')],check=True)
