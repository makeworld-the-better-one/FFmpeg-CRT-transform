#!/usr/bin/env python
## FFmpeg CRT transform script / VileR 2021
## parameter 1 = config file
## parameter 2 = input video/image
## parameter 3 = output video/image

import types
import sys
import os
from os import path
import math
import glob
import shlex
import re
import subprocess
from datetime import datetime

def run_command(command):
    try:
        process = subprocess.run(shlex.split(command, posix=(os.name == "posix")),
                                 capture_output=True,
                                 encoding="UTF-8",
                                 shell=False,
                                 check=True)
        return process.stdout
    except subprocess.CalledProcessError as e:
        print(f"Failed to run {command}, return code: {e.returncode}:", file=sys.stderr)
        print(e.stderr, file=sys.stderr)
        sys.exit(1)

def run_command_get_params(command, *params):
    result = {}
    for param in params:
        for line in run_command(command).split("\n"):
            if line.startswith(f"{param}="):
                result[param] = line.split("=")[1]
    return result

def to_python_type(token):
    if token.lower() == "yes":
        return True
    elif token.lower() == "no":
        return False

    # fractions
    fraction_re = re.match("(\d+)\s*?/\s*?(\d+)", token.lower())
    if fraction_re:
        frac = fraction_re.groups()
        return float(frac[0]) / float(frac[1])

    try:
        return float(token)
    except ValueError:
        return token.lower()

def try_remove(filename):
    try:
        os.remove(filename)
    except FileNotFoundError:
        pass

##+++++++++++##
## Constants ##
##+++++++++++##

loglevel = "error"

##+++++++++++++++++++++++++##
## Check cmdline arguments ##
##+++++++++++++++++++++++++##

if len(sys.argv) < 3:
  print(f"""FFmpeg CRT transform script / VileR 2021
       USAGE: {sys.argv[0]} <config_file> <input_file> [output_file]
   input_file must be a valid image or video.  If output_file is omitted, the
   output will be named "(input_file)_(config_file).(input_ext)""")
  sys.exit(0)

config_file = sys.argv[1]
input_file = sys.argv[2]
output_file = sys.argv[3] if len(sys.argv) > 3 else \
    "%s.%s.%s".format(path.splitext(sys.argv[1])[0],
                      path.splitext(sys.argv[2])[0],
                      path.splitext(sys.argv[1])[1])

if not path.exists(input_file):
    print(f"Input file not found: {input_file} ", file=sys.stderr);
    sys.exit(1)

if not path.exists(config_file):
    print(f"Config file not found: {config_file}", file=sys.stderr);
    sys.exit(1)

if len(path.splitext(output_file)) < 2:
    print("Output filename must have an extension: {output_file}", file=sys.stderr)
    sys.exit(1)

##++++++++++++++++++++++++++++++++++++++++++++++##
## Find input dimensions and type (image/video) ##
##++++++++++++++++++++++++++++++++++++++++++++++##

ix = None
iy = None
fc = None

result = run_command_get_params(f"ffprobe -hide_banner -loglevel quiet -select_streams v:0 -show_entries stream=width,height,nb_frames '{input_file}'", "width", "height", "nb_frames")

ix = int(result["width"])
iy = int(result["height"])
fc = result["nb_frames"]

if None in (ix, iy, fc):
    print(f"Couldn't get media info for input file '{input_file}' (invalid image/video?)", file=sys.stderr)
    sys.exit(1)

# Matroska and webm doesn't return nb_frames; assume video
if path.splitext(input_file)[1] in (".mkv", ".webm"):
    fc = "unknown"

is_video = fc != "N/A"

##++++++++++##
## Settings ##
##++++++++++##

# Read config file / check for required external files
params = types.SimpleNamespace()
with open(config_file, "rb") as fp:
    for line in fp:
        line = str(line, encoding="UTF-8").strip()
        result = re.findall('^([^;][^\s]+)\s+([^\s]+)', line)
        if result:
            key, value = result[0]
            key = key if key[0].isalpha() else  "_" + key
            params.__dict__[key.lower()] = to_python_type(value)

overlay_file = f"_{params.ovl_type}.png"
if not os.path.exists(overlay_file):
    print(f"File not found: {overlay_file}", file=sys.stderr)

## Set temporary + final output parameters
if is_video:
    if params.oformat == 0:
        fin_outparams = "-pix_fmt rgb24 -c:a copy -c:v libx264rgb -crf 8"
        fin_matrixstr = ""
    elif params.oformat == 1:
        fin_outparams = "-pix_fmt yuv444p10le -color_primaries 1 -color_trc 1 -colorspace 1 -color_range 2 -c:v libx264 -crf 8 -c:a copy"
        fin_matrixstr = ", scale=iw:ih:flags=neighbor+full_chroma_inp:in_range=full:out_range=full:out_color_matrix=bt709"

    if params._16bpc_processing:
        tmp_ext = "mkv"
        tmp_outparams = "-pix_fmt gbrp16le -c:a copy -c:v ffv1"
    else:
        tmp_ext = "mkv"
        tmp_outparams = "-c:a copy -c:v libx264rgb -crf 0"
else:
    fin_matrixstr = ""
    if params.oformat == 0:
        fin_outparams = "-frames:v 1 -pix_fmt rgb24"
    elif params.oformat == 1:
        fin_outparams = "-frames:v 1 -pix_fmt rgb48be"

    if params._16bpc_processing:
        tmp_ext = "mkv"
        tmp_outparams = "-pix_fmt gbrp16le -c:v ffv1"
    else:
        tmp_ext = "png"
        tmp_outparams = ""

## Bit depth-dependent vars
if params._16bpc_processing:
    rng = 65536
    rgbfmt = "gbrp16le"
    kludgefmt = "gbrpf32le"
else:
    rng = 256
    rgbfmt = "rgb24"
    kludgefmt = "rgb24"

## Set some shorthand vars and calculate stuff
params.sxint = int(ix * params.prescale_by)
params.px = int(ix * params.prescale_by * params.px_aspect)
params.py = int(iy * params.prescale_by)
params.ox = f"round({params.oy}*{params.oaspect})"
params.swsflags = "accurate_rnd+full_chroma_int+full_chroma_inp"
params.bezel_curvature = 0
params.lensc = ""
params.bzlensc = ""

params.vsigma = 0.1 if params.v_px_blur == 0 else f"{params.v_px_blur}/100*{params.prescale_by}"

if params.vignette_on:
    if params._16bpc_processing:
        params.vignette_str = f"""
        [ref]; color=c=#FFFFFF:s={params.px}x{params.py},format=rgb24[mkscale];^
        [mkscale][ref]scale2ref=flags=neighbor[mkvig][novig];^
        [mkvig]setsar=sar=1/1, vignette=PI*{params.vignette_power},format=gbrp16le[vig];^
        [novig][vig]blend=all_mode='multiply':shortest=1,"""
    else:
        params.vignette_str = f", vignette=PI*{params.vignette_power},"
else:
    params.vignette_str = ","

if params.flat_panel:
    params.scanlines_on = False
    params.crt_curvature = 0
    params.ovl_alpha = 0

## Curvature factors
if float(params.bezel_curvature) < float(params.crt_curvature):
    params.bezel_curvature = params.crt_curvature

if params.crt_curvature != 0:
    params.lensc = f", pad=iw+8:ih+8:4:4:black, lenscorrection=k1={params.crt_curvature}:k2={params.crt_curvature}:i=bilinear, crop=iw-8:ih-8"

if params.bezel_curvature != 0:
    params.bzlensc = f", scale=iw*2:ih*2:flags=gauss, pad=iw+8:ih+8:4:4:black, lenscorrection=k1={params.bezel_curvature}:k2={params.bezel_curvature}:i=bilinear, crop=iw-8:ih-8, scale=iw/2:ih/2:flags=gauss"

## Scan factor
if params.scan_factor == "half":
    params.scan_factor = 0.5
    params.sl_count = int(iy / 2)
elif params.scan_factor == "double":
    params.scan_factor = 2
    params.sl_count = iy * 2
else:
    params.scan_factor = 1
    params.sl_count = iy

## Handle monochrome settings; special cases: 'p7' (decay/latency are processed differently and require a couple more curve maps),
## 'paperwhite' (uses a texture overlay), 'lcd*' (optional texture overlay + if FLAT_PANEL then pixel grid is inverted too)

params.texture_ovl = None
params.pxgrid_invert = False
params.monocurves = ""

if params.monitor_color == "white":
    params.monocurves = ""
elif params.monitor_color == "paperwhite":
    params.monocurves = ""
    params.texture_ovl = "paper"
elif params.monitor_color == "green1":
    params.monocurves = "curves=r='0/0 .77/0 1/.45':g='0/0 .77/1 1/1':b='0/0 .77/.17 1/.73',"
elif params.monitor_color == "green2":
    params.monocurves = "curves=r='0/0 .43/.16 .72/.30 1/.56':g='0/0 .51/.53 .82/1 1/1':b='0/0 .43/.16 .72/.30 1/.56',"
elif params.monitor_color == "bw-tv":
    params.monocurves = "curves=r='0/0 .5/.49 1/1':g='0/0 .5/.49 1/1':b='0/0 .5/.62 1/1',"
elif params.monitor_color == "amber":
    params.monocurves = "curves=r='0/0 .25/.45 .8/1 1/1':g='0/0 .25/.14 .8/.55 1/.8':b='0/0 .8/0 1/.29',"
elif params.monitor_color == "plasma":
    params.monocurves = "curves=r='0/0 .13/.27 .52/.83 .8/1 1/1':g='0/0 .13/0 .52/.14 .8/.35 1/.54':b='0/0 1/0',"
elif params.monitor_color == "eld":
    params.monocurves = "curves=r='0/0 .46/.49 1/1':g='0/0 .46/.37 1/.94':b='0/0 .46/0 1/.29',"
elif params.monitor_color == "lcd":
    params.monocurves = "curves=r='0/.09 1/.48':g='0/.11 1/.56':b='0/.20 1/.35',"
elif params.monitor_color == "lcd-lite":
    params.monocurves = "curves=r='0/.06 1/.64':g='0/.15 1/.77':b='0/.35 1/.65',"
elif params.monitor_color == "lcd-lwhite":
    params.monocurves = "curves=r='0/.09 1/.82':g='0/.18 1/.89':b='0/.29 1/.93',"
elif params.monitor_color == "lcd-lblue":
    params.monocurves = "curves=r='0/.00 1/.62':g='0/.22 1/.75':b='0/.73 1/.68',"

if params.monitor_color.startswith("lcd"):
    params.pxgrid_invert = True
    ## allow lcd grain only for the appropriate monitor types
    if params.lcd_grain > 0:
        params.texture_ovl = "lcdgrain"

params.mono_str1 = ""
params.mono_str2 = ""

if params.monitor_color != "rgb":
    params.ovl_alpha = 0
    params.mono_str1 = "format=gray16le,format=gbrp16le,"
    params.mono_str2 = params.monocurves

if params.monitor_color == "p7":
    params.monocurves_lat = "curves=r='0/0 .6/.31 1/.75':g='0/0 .25/.16 .75/.83 1/.94':b='0/0 .5/.76 1/.97'"
    params.monocurves_dec = "curves=r='0/0 .5/.36 1/.86':g='0/0 .5/.52 1/.89':b='0/0 .5/.08 1/.13'"
    params.decaydelay = params.latency / 2

    if is_video:
        params.mono_str2 = f"""split=4 [orig][a][b][c];
                [a] tmix={params.latency}, {params.monocurves_lat} [lat];
                [b] lagfun={params.p_decay_factor} [dec1]; [c] lagfun={params.p_decay_factor}*0.95 [dec2];
                [dec2][dec1] blend=all_mode='lighten':all_opacity=0.3, {params.monocurves_dec}, setpts=PTS+(params.{decaydelay}/FR)/TB [decay];
                [lat][decay] blend=all_mode='lighten':all_opacity={params.p_decay_alpha} [p7];
                [orig][p7] blend=all_mode='screen',format={rgbfmt},"""
    else:
        params.mono_str2 = f"""split=3 [orig][a][b];
                [a] {params.monocurves_lat} [lat];
                [b] {params.monocurves_dec} [decay];
                [lat][decay] blend=all_mode='lighten':all_opacity={params.p_decay_alpha} [p7];
                [orig][p7] blend=all_mode='screen',format={rgbfmt},"""

# Can skip some stuff where not needed
params.skip_ovl = params.ovl_alpha == 0.0
params.skip_bri = params.brighten == 1.0

ffstart = datetime.now()
if fc != "N/A":
    print(f"Input frame count: {fc}")

##+++++++++++++++++++++++++++++++++++++++++++++++++##
## Create bezel with rounded corners and curvature ##
##+++++++++++++++++++++++++++++++++++++++++++++++++##

print("Bezel")
if params.corner_radius == 0:
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y
        -f lavfi -i "color=c=#ffffff:s={params.px}x{params.py}, format=rgb24 {params.bzlensc}"
        -frames:v 1 TMPbezel.png''')
else:
        run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y
        -f lavfi -i "color=s=1024x1024, format=gray, geq='lum=if(lte((X-W)^2+(Y-H)^2, 1024*1024), 255, 0)', scale={params.corner_radius}:{params.corner_radius}:flags=lanczos"
        -filter_complex "
                color=c=#ffffff:s={params.px}x{params.py}, format=rgb24[bg];
                [0] split=4 [tl][c2][c3][c4];
                [c2] transpose=1 [tr];
                [c3] transpose=3 [br];
                [c4] transpose=2 [bl];
                [bg][tl] overlay=0:0:format=rgb [p1];
                [p1][tr] overlay={params.px}-{params.corner_radius}:0:format=rgb [p2];
                [p2][br] overlay={params.px}-{params.corner_radius}:{params.py}-{params.corner_radius}:format=rgb [p3];
                [p3][bl] overlay=x=0:y={params.py}-{params.corner_radius}:format=rgb {params.bzlensc}"
       -frames:v 1 TMPbezel.png''')

##+++++++++++++++++++++++++++++++++##
## Create scanlines, add curvature ##
##+++++++++++++++++++++++++++++++++##

if params.scanlines_on:
    print("Scanlines")
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -f lavfi
        -i nullsrc=s=1x100
        -vf "
                format=gray,
                geq=lum='if(lt(Y,{params.prescale_by}/{params.scan_factor}), pow(sin(Y*PI/({params.prescale_by}/{params.scan_factor})), 1/{params.sl_weight})*255, 0)',
                crop=1:{params.prescale_by}/{params.scan_factor}:0:0,
                scale={params.px}:ih:flags=neighbor"
        -frames:v 1 TMPscanline.png''')

    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -loop 1 -framerate 1 -t {params.sl_count}
        -i TMPscanline.png
        -vf "
                format=gray16le,
                tile=layout=1x{params.sl_count},
                scale=iw*3:ih*3:flags=gauss {params.lensc}, scale=iw/3:ih/3:flags=gauss,
                format=gray16le, format={rgbfmt}"
        -frames:v 1 {tmp_outparams} TMPscanlines.{tmp_ext}''')

##**************************************************##
## Create shadowmask/texture overlay, add curvature ##
##**************************************************##

print("Shadowmask overlay")
if params.ovl_alpha > 0:
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -i _{params.ovl_type}.png -vf "
                lutrgb='r=gammaval(2.2):g=gammaval(2.2):b=gammaval(2.2)',
                scale=round(iw*{params.ovl_scale}):round(ih*{params.ovl_scale}):flags=lanczos+{params.swsflags}"
        TMPshadowmask1x.png''')

    result = run_command_get_params("ffprobe -hide_banner -loglevel quiet -show_entries stream=width,height TMPshadowmask1x.png", "width", "height")

    params.ovl_x = int(result["width"])
    params.ovl_y = int(result["height"])
    params.tiles_x = int(params.px / params.ovl_x + 1)
    params.tiles_y = int(params.py / params.ovl_y + 1)

    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -loop 1 -i TMPshadowmask1x.png -vf "
      tile=layout={params.tiles_x}x{params.tiles_y},
      crop={params.px}:{params.py},
      scale=iw*2:ih*2:flags=gauss {params.lensc},
      scale=iw/2:ih/2:flags=bicubic,
      lutrgb='r=gammaval(0.454545):g=gammaval(0.454545):b=gammaval(0.454545)'"
  -frames:v 1 TMPshadowmask.png''')
else:
    # (if shadowmask alpha is 0, just make a blank canvas)
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -f lavfi -i "color=c=#00000000:s={params.px}x{params.py},format=rgba" -frames:v 1 TMPshadowmask.png''')

    if params.texture_ovl == "paper":
        # Phosphor overlay for monochrome "paper white" only, doesn't need curvature
        params.paperx = int(params.oy * params.oaspect * 67 / 100)
        params.papery = int(params.oy * 67 / 100)

        print("Texture overlay")
        run_command(f'''ffmpeg -hide_banner -y -loglevel {loglevel} -stats -f lavfi -i "color=c=#808080:s={params.paperx}x{params.papery}"
        -filter_complex "
                noise=all_seed=5150:all_strength=100:all_flags=u, format=gray,
                lutrgb='r=(val-70)*255/115:g=(val-70)*255/115:b=(val-70)*255/115',
                format=rgb24,
                lutrgb='
                        r=if(between(val,0,101),207,if(between(val,102,203),253,251)):
                        g=if(between(val,0,101),238,if(between(val,102,203),225,204)):
                        b=if(between(val,0,101),255,if(between(val,102,203),157,255))',
                format=gbrp16le,
                lutrgb='r=gammaval(2.2):g=gammaval(2.2):b=gammaval(2.2)',
                scale={params.ox}:{params.oy}:flags=bilinear,
                gblur=sigma=3:steps=6,
                lutrgb='r=gammaval(0.454545):g=gammaval(0.454545):b=gammaval(0.454545)',
                format=gbrp16le,format=rgb24
        " -frames:v 1 TMPtexture.png''')
    elif params.texture_ovl:
        # Otherwise texture_ovl == lcdgrain here
        # LCD grain overlay (for lcd* monitor types only), doesn't need curvature
        params.grainx = int(params.oy * params.oaspect * 50 / 100)
        params.grainy = int(params.oy * 50 / 100)

        print("Texture overlay")
        run_command(f'''ffmpeg -hide_banner -y -loglevel {loglevel} -stats -filter_complex "color=#808080:s={params.grainx}x{params.grainy},
                noise=all_seed=5150:all_strength={params.lcd_grain}, format=gray,
                scale={params.ox}:{params.oy}:flags=lanczos, format=rgb24
        " -frames:v 1 TMPtexture.png''')

##+++++++++++++++++++++++++++++++++++##
## Create discrete pixel grid if set ##
##+++++++++++++++++++++++++++++++++++##

if params.flat_panel:
    params.lum_gap = 255 - 255 * params.pxgrid_alpha
    params.lum_px = 255

    params.lum_gap = 255 * params.pxgrid_alpha if params.pxgrid_invert else 0
    params.gx = int(params.prescale_by / params.px_factor_x)
    params.gy = params.prescale_by / params.px_factor_y

    print("Grid")
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -f lavfi
    -i nullsrc=s={params.sxint}x{params.py} -vf "
        format=gray,
        geq=lum='if(gte(mod(X,{params.gx}),{params.gx}-{params.px_x_gap})+gte(mod(Y,{params.gy}),{params.gy}-{params.px_y_gap}),{params.lum_gap},{params.lum_px})',
        format=gbrp16le,
        lutrgb='r=gammaval(2.2):g=gammaval(2.2):b=gammaval(2.2)',
        scale={params.px}:ih:flags=bicubic,
        lutrgb='r=gammaval(0.454545):g=gammaval(0.454545):b=gammaval(0.454545)',
        format=gbrp16le,format=rgb24"
    -frames:v 1 TMPgrid.png''')

##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##
## Pre-process if needed: phosphor decay (video only), invert, pixel latency (video only) ##
##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##

params.scalesrc = input_file
params.preprocess = False
params.vf_invert = False
params.vf_decay = False
params.vf_pre = ""

if params.invert_input:
    params.preprocess = True
    params.vf_pre = "negate"

if is_video and params.latency > 0 and params.monitor_color != "p7":
    params.preprocess = True
    if params.vf_pre:
        params.vf_pre = f", {params.vf_pre}"
    params.vf_pre = f"""split [o][2lat];
                [2lat] tmix={params.latency}, setpts=PTS+(({params.latency}/2)/FR)/TB [lat];
                [lat][o] blend=all_opacity={params.latency_alpha}
                {params.vf_pre}"""

if is_video and params.p_decay_factor > 0 and params.monitor_color != "p7":
    params.preprocess = True
    if params.vf_pre:
        params.vf_pre = f", {params.vf_pre}"
    params.vf_pre = f"""[0] split [orig][2lag];
            [2lag] lagfun={params.p_decay_factor} [lag];
            [orig][lag] blend=all_mode='lighten':all_opacity={params.p_decay_alpha}
            {params.vf_pre}"""

if params.preprocess:
    print("Step00 (preprocess)")
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -i '{input_file}' -filter_complex "{params.vf_pre}" {tmp_outparams} TMPstep00.{tmp_ext}''')

    params.scalesrc = f"TMPstep00.{tmp_ext}"

##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##
## Scale nearest neighbor, go 16bit/channel, apply grid, gamma & pixel blur ##
##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##

# If we have a grid, the inputs + first part of filter are different
params.gridblendmode = "screen" if params.pxgrid_invert else 'multiply'
params.gridfilterfrag = f"[scaled]; movie=TMPgrid.png[grid]; [scaled][grid]blend=all_mode={params.gridblendmode}" if params.flat_panel else ""

print("Step01")
run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -i {params.scalesrc} -filter_complex "
        scale=iw*{params.prescale_by}:ih:flags=neighbor,
        format=gbrp16le,
        lutrgb='r=gammaval(2.2):g=gammaval(2.2):b=gammaval(2.2)',
        scale=iw*{params.px_aspect}:ih:flags=fast_bilinear,
        scale=iw:ih*{params.prescale_by}:flags=neighbor
        {params.gridfilterfrag},
        gblur=sigma={params.h_px_blur}/100*{params.prescale_by}*{params.px_aspect}:sigmaV={params.vsigma}:steps=3"
-c:v ffv1 -c:a copy TMPstep01.mkv''')

##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##
## Add halation, revert gamma, normalize blackpoint, revert bit depth, add curvature ##
##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##

# (Real halation should come after scanlines & before shadowmask, but that turned out ugly)

print("Step02")
if params.halation_on:
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -i TMPstep01.mkv -filter_complex "
                [0]split[a][b],
                [a]gblur=sigma={params.halation_radius}:steps=6[h],
                [b][h]blend=all_mode='lighten':all_opacity={params.halation_alpha},
                lutrgb='r=clip(gammaval(0.454545)*(258/256)-2*256 ,minval,maxval):
                        g=clip(gammaval(0.454545)*(258/256)-2*256 ,minval,maxval):
                        b=clip(gammaval(0.454545)*(258/256)-2*256 ,minval,maxval)',
                lutrgb='r=val+({params.blackpoint}*256*(maxval-val)/maxval):g=val+({params.blackpoint}*256*(maxval-val)/maxval):b=val+({params.blackpoint}*256*(maxval-val)/maxval)',
                format={rgbfmt}
                {params.lensc}"
        {tmp_outparams} TMPstep02.{tmp_ext}''')

else:
    run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -i TMPstep01.mkv -vf "
                lutrgb='r=gammaval(0.454545):g=gammaval(0.454545):b=gammaval(0.454545)',
                lutrgb='r=val+({params.blackpoint}*256*(maxval-val)/maxval):g=val+({params.blackpoint}*256*(maxval-val)/maxval):b=val+({params.blackpoint}*256*(maxval-val)/maxval)',
                format={rgbfmt}
                {params.lensc}"
       {tmp_outparams} TMPstep02.{tmp_ext}''')

##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##
## Add bloom, scanlines, shadowmask, rounded corners + brightness fix ##
##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##

if params.bezel_curvature != params.crt_curvature or params.corner_radius != 0 or not params.skip_ovl or not params.skip_bri:
    if params.scanlines_on:
        params.sl_input = f"TMPscanlines.{tmp_ext}"
        if params.bloom_on:
            params.sl_input = f"TMPbloom.{tmp_ext}"
            print("Step02-bloom")
            run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y
                -i TMPscanlines.{tmp_ext} -i TMPstep02.{tmp_ext} -filter_complex "
                      [1]lutrgb='r=gammaval(2.2):g=gammaval(2.2):b=gammaval(2.2)', hue=s=0, lutrgb='r=gammaval(0.454545):g=gammaval(0.454545):b=gammaval(0.454545)'[g],
                   [g][0]blend=all_expr='if(gte(A,{rng}/2), (B+({rng}-1-B)*{params.bloom_power}*(A-{rng}/2)/({rng}/2)), B)',
                   setsar=sar=1/1"
                 {tmp_outparams} {params.sl_input}''')

        print("Step03")
        run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y
        -i TMPstep02.{tmp_ext} -i {params.sl_input} -i TMPshadowmask.png -i TMPbezel.png -filter_complex "
                [0][1]blend=all_mode='multiply':all_opacity={params.sl_alpha}[a],
                [a][2]blend=all_mode='multiply':all_opacity={params.ovl_alpha}[b],
                [b][3]blend=all_mode='multiply',
                lutrgb='r=clip(val*{params.brighten},0,{rng}-1):g=clip(val*{params.brighten},0,{rng}-1):b=clip(val*{params.brighten},0,{rng}-1)'"
    {tmp_outparams} TMPstep03.{tmp_ext}''')

    else:
        print("Step03")
        run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y
        -i TMPstep02.{tmp_ext} -i TMPshadowmask.png -i TMPbezel.png -filter_complex "
                [0][1]blend=all_mode='multiply':all_opacity={params.ovl_alpha}[b],
                [b][2]blend=all_mode='multiply',
                lutrgb='r=clip(val*{params.brighten},0,{rng}-1):g=clip(val*{params.brighten},0,{rng}-1):b=clip(val*{params.brighten},0,{rng}-1)'"
        {tmp_outparams} TMPstep03.{tmp_ext}''')

# Can be skipped if none of the above are needed
else:
    os.rename(f"TMPstep02.{tmp_ext}", f"TMPstep03.{tmp_ext}")

##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##
## Detect crop area; crop, rescale, monochrome (if set), vignette, pad, set sar/dar ##
##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++##

crop_output = run_command_get_params(f'''ffmpeg -hide_banner -y
        -f lavfi -i "color=c=#ffffff:s={params.px}x{params.py}" -i TMPbezel.png
        -filter_complex "[0]format=rgb24 {params.lensc}[crt]; [crt][1]overlay, cropdetect=limit=0:round=2"
        -frames:v 3 -f null''')

print("CROP_OUTPUT")
print()
print(crop_output)
print()

params.crop_str = ""

if params.texture_ovl == "paper":
    params.texture_str = f"[nop];movie=TMPtexture.png,format={rgbfmt}[paper];[nop][paper]blend=all_mode='multiply':eof_action='repeat'"
elif params.texture_ovl == "lcdgrain":
    params.texture_str = f""",format={kludgefmt},split[og1][og2];
        movie=TMPtexture.png,format={kludgefmt}[lcd];
        [lcd][og1]blend=all_mode='vividlight':eof_action='repeat'[notquite];
        [og2]limiter=0:110*{rng}/256[fix];
        [fix][notquite]blend=all_mode='lighten':eof_action='repeat', format={rgbfmt}"""
else:
    params.texture_str = ""

print("Output")
run_command(f'''ffmpeg -hide_banner -loglevel {loglevel} -stats -y -i TMPstep03.{tmp_ext} -filter_complex "
        crop={params.crop_str},
        format=gbrp16le,
        lutrgb='r=gammaval(2.2):g=gammaval(2.2):b=gammaval(2.2)',
        {params.mono_str1}
        scale=w={params.ox}-{params.omargin}*2:h={params.oy}-{params.omargin}*2:force_original_aspect_ratio=decrease:flags={params.ofilter}+{params.swsflags},
        lutrgb='r=gammaval(0.454545):g=gammaval(0.454545):b=gammaval(0.454545)',
        format=gbrp16le,
        format={rgbfmt},
        {params.mono_str2}
        setsar=sar=1/1
        {params.vignette_str}
        pad={params.ox}:{params.oy}:-1:-1:black
        {params.texture_str}
        {fin_matrixstr}"
{fin_outparams} {output_file}''')

##++++++++++##
## Clean up ##
##++++++++++##

for file in glob.glob("TMP*"):
    try_remove(file)

ffduration = datetime.now() - ffstart

print(f"""
Output file: {output_file}
Started:     {ffstart}
Finished:    {ffduration}""")
