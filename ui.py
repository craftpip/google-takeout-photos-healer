import tkinter as tk
from tkinter import ttk, filedialog

def browse_file():
    path = filedialog.askopenfilename()
    input_file_var.set(path)

def run_task():
    output_box.insert(tk.END, "Run clicked\n")
    progress['value'] = 35  # set progress to 35%

def stop_task():
    output_box.insert(tk.END, "Stop clicked\n")

# ─── Main Window ──────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Media Tool")
root.geometry("600x450")

# ─── Variables ────────────────────────────────────────────────────────────────
input_file_var = tk.StringVar()
write_mode_var = tk.BooleanVar()
date_var = tk.StringVar()

# ─── Input File ───────────────────────────────────────────────────────────────
tk.Label(root, text="Input File").pack(anchor="w", padx=10, pady=(10, 0))

file_frame = tk.Frame(root)
file_frame.pack(fill="x", padx=10)

tk.Entry(file_frame, textvariable=input_file_var).pack(side="left", fill="x", expand=True)
tk.Button(file_frame, text="Browse", command=browse_file).pack(side="left", padx=5)

# ─── Checkbox ─────────────────────────────────────────────────────────────────
tk.Checkbutton(
    root,
    text="Write mode",
    variable=write_mode_var
).pack(anchor="w", padx=10, pady=10)

# ─── Date Input ───────────────────────────────────────────────────────────────
tk.Label(root, text="Date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)").pack(anchor="w", padx=10)
tk.Entry(root, textvariable=date_var).pack(fill="x", padx=10)

# ─── Run / Stop Buttons ───────────────────────────────────────────────────────
btn_frame = tk.Frame(root)
btn_frame.pack(pady=15)

tk.Button(btn_frame, text="Run", width=12, command=run_task).pack(side="left", padx=5)
tk.Button(btn_frame, text="Stop", width=12, command=stop_task).pack(side="left", padx=5)

# ─── Progress Bar ─────────────────────────────────────────────────────────────
progress = ttk.Progressbar(root, length=400, mode="determinate", maximum=100)
progress.pack(pady=10)
progress['value'] = 35

# ─── Output Box ───────────────────────────────────────────────────────────────
tk.Label(root, text="Output").pack(anchor="w", padx=10)

output_box = tk.Text(root, height=8)
output_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

# ─── Start UI ─────────────────────────────────────────────────────────────────
root.mainloop()
