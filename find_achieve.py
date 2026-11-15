import sys, os
d = r"C:\Users\Admin\.gemini\antigravity\brain\443c207e-98dc-404d-818e-8c769770e691\swf_libs"
for root, dirs, files in os.walk(d):
    for f in files:
        if f.endswith(".asasm"):
            p = os.path.join(root, f)
            t = open(p, "r", encoding="utf-8", errors="ignore").read()
            if "2ndpass" in t:
                print(f)
                for line in t.splitlines():
                    if "2ndpass" in line:
                        print("  ", line.strip())
