
import sys
import logging
from dataclasses import dataclass
from unittest.mock import MagicMock

# Mock dependencies
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["core.config"] = MagicMock()

# Mock settings object
mock_settings = MagicMock()
mock_settings.output_fps = 30
mock_settings.ffmpeg_path = "ffmpeg"
mock_settings.video_bitrate = "5M"
mock_settings.audio_bitrate = "192k"
sys.modules["core.config"].settings = mock_settings

# Add the project root to sys.path
sys.path.append("/Users/fernando/Documents/api-cortes")

# Now import the module under test
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
        # We only need to test build_filter_complex which returns the string
        fc, v, a = build_filter_complex(segments, opts, fps)
        print("Generated Filter Complex:")
        print(fc)
        
        success = True
        if "color=pink" in fc:
            print("\nSUCCESS: 'color=pink' found in filter complex.")
        else:
            print("\nFAILURE: 'color=pink' NOT found in filter complex.")
            success = False
            
        if "type=pink" in fc:
             print("FAILURE: 'type=pink' still found in filter complex.")
             success = False
        else:
             print("SUCCESS: 'type=pink' NOT found in filter complex.")

        if not success:
            sys.exit(1)

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    verify_fix()
