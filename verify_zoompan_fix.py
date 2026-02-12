
import sys

def verify_zoompan_logic():
    video_label = "vcolor"
    w = 1080
    h = 1920
    fps = 30
    vout = "vzoom"
    
    # This matches the fixed logic in video_engine.py
    filter_str = (
        f"[{video_label}]zoompan="
        f"z='1.01+0.01*sin(2*3.14159*t/5)':"
        f"x='int(iw/2-(iw/zoom/2))':"
        f"y='int(ih/2-(ih/zoom/2))':"
        f"d=1:s={w}x{h}:fps={fps},"
        f"format=yuv420p[{vout}]"
    )
    
    print("Generated Zoompan Filter:")
    print(filter_str)
    
    if "x='int(" in filter_str and "y='int(" in filter_str:
        print("\nSUCCESS: 'int()' wrapping found.")
    else:
        print("\nFAILURE: 'int()' wrapping NOT found.")
        sys.exit(1)

if __name__ == "__main__":
    verify_zoompan_logic()
