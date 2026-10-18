"""One 45-second Russian clip; one GPU model per process, no package installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import nvidia.cublas
import nvidia.cudnn
from faster_whisper import WhisperModel

parser=argparse.ArgumentParser()
parser.add_argument('model',choices=['large-v3-turbo','medium'])
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
out=root/'artifacts/hermes_cycle_20261018/analysis'
out.mkdir(parents=True, exist_ok=True)
audio=out/'russian-30-75s.wav'
if not audio.exists():
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ss','30','-i',
        str(root/'artifacts/mvp_02_03/video_probe/audio_16khz.wav'),'-t','45',
        '-ac','1','-ar','16000',str(audio)],check=True)
handles=[]
for package in [nvidia.cublas,nvidia.cudnn]:
    folder=Path(package.__path__[0])/'bin'
    if folder.is_dir():
        handles.append(os.add_dll_directory(str(folder)))
        os.environ['PATH']=str(folder)+os.pathsep+os.environ.get('PATH','')
gpu_samples=[]
stop=threading.Event()
def monitor():
    while not stop.is_set():
        try:
            value=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],text=True)
            gpu_samples.append(int(value.splitlines()[0]))
        except Exception:
            pass
        stop.wait(1)
thread=threading.Thread(target=monitor,daemon=True)
thread.start()
started=time.monotonic()
result={'model':args.model,'device':'cuda','compute_type':'int8_float16','input_seconds':45,
        'source_segment_seconds':[30,75],'audio_sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),
        'human_reference':None,'quality_metric':'No WER without human-checked reference; review terms/numbers explicitly.'}
try:
    model=WhisperModel(args.model,device='cuda',compute_type='int8_float16',
        download_root=str(root/'artifacts/models'),num_workers=1)
    result['load_and_download_seconds']=round(time.monotonic()-started,2)
    inference=time.monotonic()
    segments,info=model.transcribe(str(audio),language='ru',beam_size=3,
        temperature=0,condition_on_previous_text=False,vad_filter=True)
    result['segments']=[{'start':round(s.start,2),'end':round(s.end,2),'text':s.text.strip()} for s in segments]
    result['inference_seconds']=round(time.monotonic()-inference,2)
    result['language']=info.language
    result['status']='TRANSCRIBED_REQUIRES_TERM_REVIEW'
except Exception as e:
    result['status']='FAILED'
    result['error']=type(e).__name__+':'+str(e)[:400]
finally:
    stop.set()
    thread.join(timeout=2)
    result['observed_gpu_memory_mib_max']=max(gpu_samples,default=None)
    result['wall_seconds']=round(time.monotonic()-started,2)
    (out/f'whisper-{args.model}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='segments'},ensure_ascii=True))
