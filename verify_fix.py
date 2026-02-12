
import sys
import logging
from dataclasses import dataclass

# Add the project root to sys.path
sys.path.append("/Users/fernando/Documents/api-cortes")

from services.video_engine import build_filter_complex, Segment, VideoOptions

def verify_fix():
    segments = [Segment(0.0, 10.0)]
    opts = VideoOptions(
        layout="blur_zoom",
        zoom_level=1400,
        mirror=True,
        speed=1.07,
        color_filter=True,
        pitch_shift=1.03,
        background_noise=0.03,
        ghost_effect=True,
        dynamic_zoom=True
    )
    fps = 30
    
    try:
        fc, v, a = build_filter_complex(segments, opts, fps)
        print("Generated Filter Complex:")
        print(fc)
        
        if "color=pink" in fc:
            print("\nSUCCESS: 'color=pink' found in filter complex.")
        else:
            print("\nFAILURE: 'color=pink' NOT found in filter complex.")
            
        if "type=pink" in fc:
             print("FAILURE: 'type=pink' still found in filter complex.")
        else:
             print("SUCCESS: 'type=pink' NOT found in filter complex.")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    verify_fix()
