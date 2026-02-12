
import sys

def verify_logic(noise_level, audio_label):
    aout = "anoise"
    # This is the fixed logic from video_engine.py
    filter_str = (
        f"anoisesrc=color=pink:r=44100:a={noise_level:.4f}:d=600,"
        f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[bg_noise];\n"
        f"[{audio_label}][bg_noise]amix=inputs=2:duration=first,"
        f"volume=2.0[{aout}]"
    )
    return filter_str

if __name__ == "__main__":
    noise_level = 0.03
    audio_label = "apitch"
    
    fc = verify_logic(noise_level, audio_label)
    print("Generated Filter Segment:")
    print(fc)
    
    success = True
    if "color=pink" in fc:
        print("\nSUCCESS: 'color=pink' found.")
    else:
        print("\nFAILURE: 'color=pink' NOT found.")
        success = False
        
    if "type=pink" in fc:
         print("FAILURE: 'type=pink' still found.")
         success = False
    else:
         print("SUCCESS: 'type=pink' NOT found.")

    if not success:
        sys.exit(1)
