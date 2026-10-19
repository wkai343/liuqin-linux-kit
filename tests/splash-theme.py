#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the theme with the installed Plymouth interpreter and in-memory display mocks.

Run one viewport/angle per process. No daemon, VT, input or display device is opened.
Requires Fedora's plymouth-plugin-script and plymouth-theme-spinner packages.
"""
from pathlib import Path
import argparse,ctypes as C,ctypes.util,json,hashlib,os,tempfile
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--template',type=Path,default=Path(__file__).resolve().parents[1]/'device/boot/splash/theme.script.in')
parser.add_argument('--plugin',type=Path,default=Path('/usr/lib64/plymouth/script.so'))
parser.add_argument('--assets',type=Path,default=Path('/usr/share/plymouth/themes/spinner'))
parser.add_argument('--width',type=int,choices=(900,1800),required=True)
parser.add_argument('--angle',type=int,choices=(0,90,180,270),required=True)
args=parser.parse_args()
width=args.width;height=width*8//5;angle=args.angle;plugin=args.plugin
source=args.template.read_text().replace('@ROTATION@',str(angle))
class State(C.Structure):_fields_=[('user',C.c_void_p),('global_',C.c_void_p),('local',C.c_void_p),('this',C.c_void_p)]
class Return(C.Structure):_fields_=[('kind',C.c_int),('obj',C.c_void_p)]
class ImageData(C.Structure):_fields_=[('class_',C.c_void_p),('op',C.c_void_p),('directory',C.c_char_p)]
class Rectangle(C.Structure):_fields_=[('x',C.c_long),('y',C.c_long),('width',C.c_ulong),('height',C.c_ulong)]
lib=C.CDLL(str(plugin));core=C.CDLL(ctypes.util.find_library('ply-splash-core'))
def bind(obj,name,args,result):
    f=getattr(obj,name);f.argtypes=args;f.restype=result;return f
new=bind(lib,'script_state_new',[C.c_void_p],C.c_void_p)
setup_math=bind(lib,'script_lib_math_setup',[C.c_void_p],C.c_void_p)
setup_image=bind(lib,'script_lib_image_setup',[C.c_void_p,C.c_char_p],C.c_void_p)
parse=bind(lib,'script_parse_file',[C.c_char_p],C.c_void_p)
execute=bind(lib,'script_execute',[C.c_void_p,C.c_void_p],Return)
number=bind(lib,'script_obj_hash_get_number',[C.c_void_p,C.c_char_p],C.c_double)
native=bind(lib,'script_obj_hash_get_native_of_class',[C.c_void_p,C.c_char_p,C.c_void_p],C.c_void_p)
size=bind(core,'ply_pixel_buffer_get_size',[C.c_void_p,C.POINTER(Rectangle)],None)
pixels=bind(core,'ply_pixel_buffer_get_argb32_data',[C.c_void_p],C.POINTER(C.c_uint32))
state=new(None);assert state and setup_math(state)
image_data=setup_image(state,os.fsencode(args.assets));assert image_data
prelude=f'mock_width={width}; mock_height={height};\n'+r'''
failures=0; checks=0; completed=0;
fun Check(value) { global.checks++; if (!value) global.failures++; }
Window=[];
Window.GetWidth=fun() { return mock_width; };
Window.GetHeight=fun() { return mock_height; };
Window.GetX=fun() { return 0; };
Window.GetY=fun() { return 0; };
Window.SetBackgroundTopColor=fun(r,g,b) { return 0; };
Window.SetBackgroundBottomColor=fun(r,g,b) { return 0; };
Sprite=fun(image) {
    local.s=[]; s.image=image; s.opacity=1;
    s.SetImage=fun(image) { this.image=image; };
    s.SetOpacity=fun(value) { this.opacity=value; };
    s.SetPosition=fun(x,y,z) { this.x=x; this.y=y; this.z=z; };
    return s;
};
Plymouth=[];
Plymouth.SetRefreshRate=fun(rate) { global.refresh_rate=rate; };
Plymouth.SetRefreshFunction=fun(cb) { global.refresh_cb=cb; };
Plymouth.SetDisplayNormalFunction=fun(cb) { global.normal_cb=cb; };
Plymouth.SetDisplayPasswordFunction=fun(cb) { global.password_cb=cb; };
Plymouth.SetDisplayQuestionFunction=fun(cb) { global.question_cb=cb; };
Plymouth.SetDisplayMessageFunction=fun(cb) { global.message_cb=cb; };
Plymouth.SetHideMessageFunction=fun(cb) { global.hide_cb=cb; };
Plymouth.SetQuitFunction=fun(cb) { global.quit_cb=cb; };
'''
expected={0:(149,43,375.5,1341.12,434,992),90:(43,149,34.28,645.5,254,704),180:(149,43,375.5,55.88,434,416),270:(43,149,822.72,645.5,614,704)}[angle]
checks=r'''
Check(refresh_rate==30);
Check(logo.image.GetWidth()==EXPECTED_W*unit);
Check(logo.image.GetHeight()==EXPECTED_H*unit);
Check(Math.Abs(logo.sprite.x/unit-EXPECTED_X)<0.01);
Check(Math.Abs(logo.sprite.y/unit-EXPECTED_Y)<0.01);
Check(Math.Abs(spinner.sprite.x/unit-EXPECTED_SX)<0.01);
Check(Math.Abs(spinner.sprite.y/unit-EXPECTED_SY)<0.01);
for(i=0;i<30;i++) { Check(frames[i].GetWidth()==32*unit); Check(frames[i].GetHeight()==32*unit); }
for(i=0;i<100;i++) { refresh_cb(); Check(frame_index>=0 && frame_index<30); }
message_cb("Startup message"); Check(message.image!=NULL); Check(message.sprite.opacity==1);
hide_cb("Startup message"); Check(message.sprite.opacity==0);
password_cb("Enter test password",5); Check(mode=="password"); Check(spinner.sprite.opacity==0);
Check(prompt_item.image!=NULL); Check(entry_item.image!=NULL);
Check(entry_item.image.GetHeight()<=Window.GetHeight()*0.86);
password_cb("Enter test password",999); Check(entry_item.image.GetHeight()<=Window.GetHeight()*0.86);
password_cb("Enter test password",0); Check(entry_item.sprite.opacity==0);
question_cb("Test question","test answer"); Check(mode=="question"); Check(entry_item.image!=NULL);
normal_cb(); Check(mode=="normal"); Check(prompt_item.sprite.opacity==0); Check(entry_item.sprite.opacity==0);
quit_cb(); old_frame=frame_index; refresh_cb(); Check(frame_index==old_frame); Check(paused==1);
test_reference=Image("watermark.png").Scale(149*unit,43*unit);
test_rotated=rotate_landscape(test_reference);
completed=1;
'''
for key,value in zip(['W','H','X','Y','SX','SY'],expected):checks=checks.replace('EXPECTED_'+key,str(value))
with tempfile.TemporaryDirectory(prefix='liuqin-theme-check-') as tmp:
    path=Path(tmp)/'theme.script';path.write_text(prelude+source+checks)
    op=parse(os.fsencode(path));assert op,'theme parser rejected source'
result=execute(state,op);assert result.kind!=2,'interpreter reported failure'
g=C.cast(state,C.POINTER(State)).contents.global_
values={name:number(g,name.encode()) for name in ['failures','checks','completed','unit']}
assert values['completed']==1 and values['checks']>=180 and values['failures']==0,values
class_=C.cast(image_data,C.POINTER(ImageData)).contents.class_
def alpha(name):
    image=native(g,name.encode(),class_);assert image
    rect=Rectangle();size(image,C.byref(rect));assert 0<rect.width*rect.height<1000000
    data=pixels(image);assert data
    weights=[data[i]>>24 for i in range(rect.width*rect.height)]
    total=sum(weights);assert total>0
    return {'width':rect.width,'height':rect.height,'alpha_sum':total,
            'centroid_x':sum((i%rect.width)*a for i,a in enumerate(weights))/total,
            'centroid_y':sum((i//rect.width)*a for i,a in enumerate(weights))/total}
original=alpha('test_reference');rotated=alpha('test_rotated')
assert (rotated['width'],rotated['height'])==((original['height'],original['width']) if angle in (90,270) else (original['width'],original['height']))
assert abs(rotated['alpha_sum']-original['alpha_sum'])<=original['alpha_sum']*0.02
x,y=original['centroid_x'],original['centroid_y'];w,h=original['width'],original['height']
want={0:(x,y),90:(h-1-y,x),180:(w-1-x,h-1-y),270:(y,w-1-x)}[angle]
assert abs(rotated['centroid_x']-want[0])<1.25
assert abs(rotated['centroid_y']-want[1])<1.25
print(json.dumps({'viewport':[width,height],'actual_fedora_interpreter':True,'actual_png_and_rotation_library':True,'clockwise_degrees':angle,'pixel_direction_verified':True,'theme_sha256':hashlib.sha256(source.encode()).hexdigest(),'display_objects':'in-memory mocks only','results':values,'reference_alpha':original,'rotated_alpha':rotated},indent=2))
