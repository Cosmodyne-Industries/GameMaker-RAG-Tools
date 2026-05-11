import os
import re
import json
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from collections import OrderedDict
from anthropic import Anthropic

# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────

REMOVE_PART_TYPE    = True
MAX_OBJECTS_PER_CHUNK = 25
MAX_CHARS_PER_CHUNK = 80000
CONFIG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gml_tools_config.json")

INDEX_PREAMBLE = """=== GML PROJECT INDEX ===
This is a code index for a GameMaker Studio 2 project written in GML.
Review the index below and identify which objects/scripts you need to inspect in full.
When ready, tell the user which blocks you want using this format: obj_name:Event_0
For scripts (no events), just use the script name alone: scr_my_script
The user will retrieve those blocks and paste them into this conversation.
=========================================\n\n"""

SYSTEM = """You are a technical indexer for a GameMaker Studio 2 project written in GML.
Your output must be machine-readable and strictly consistent in format.
Every entry must follow this exact structure with no deviations:

OBJECT: [name]
Summary: [one sentence — role and purpose]
Dependencies: [comma-separated list of objects/scripts this relies on, or 'none']
Dependents: [comma-separated list of objects/scripts that rely on this, or 'none']

SCRIPT: [name]
Summary: [one sentence — role and purpose]
Dependencies: [comma-separated list of objects/scripts this relies on, or 'none']
Dependents: [comma-separated list of objects/scripts that rely on this, or 'none']

Rules:
- One blank line between entries, nothing else
- No markdown, no bullet points, no extra commentary
- Every object and script in the code dump must have an entry
- Base dependencies and dependents on the actual code, not just naming conventions
- Do not stop until every entry in this chunk is complete"""

MESSAGE_TEMPLATE = """Below is part {chunk_num} of {total_chunks} of a full GML code dump.
Read the entire chunk before writing any entries.
Index every object and script in this chunk. Do not stop until all entries are complete.\n\n"""


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_config(data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except:
        pass


# ─────────────────────────────────────────────
#  DUMP BUILDER
# ─────────────────────────────────────────────

def load_yy(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)

def strip_header(contents):
    lines = contents.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("/// @Dn"):
            return "\n".join(lines[i:])
    return contents

def clean_code(contents, remove_part_type=False):
    contents = re.sub(r'/\*.*?\*/', '', contents, flags=re.DOTALL)
    lines = contents.split("\n")
    cleaned = []
    part_type_redacted = False
    for line in lines:
        stripped = line.strip()
        if stripped == "": continue
        if stripped.startswith("//--"): continue
        if stripped.startswith("//=="): continue
        if stripped.startswith("// --"): continue
        if stripped.startswith("// =="): continue
        if stripped.startswith("// ==="): continue
        if stripped.startswith("//==="): continue
        if remove_part_type and stripped.startswith("part_type"):
            if not part_type_redacted:
                cleaned.append("// [part_type settings redacted]")
                part_type_redacted = True
            continue
        cleaned.append(line)
    return "\n".join(cleaned)

def get_resource_type(filepath):
    parts = filepath.replace("\\", "/").split("/")
    if "objects" in parts:
        return "OBJECT"
    elif "scripts" in parts:
        return "SCRIPT"
    return "OBJECT"

def build_dump(project_folder, output_folder, log):
    output_file = os.path.join(output_folder, "game_dump.txt")

    # build exclusion list from .yy files
    excluded = []
    for root, dirs, files in os.walk(project_folder):
        for file in files:
            if file.endswith(".yy"):
                filepath = os.path.join(root, file)
                try:
                    data = load_yy(filepath)
                    if "parent" in data:
                        if "To bin" in data["parent"]["name"]:
                            excluded.append(data["name"])
                except:
                    pass
    log(f"Excluded resources: {len(excluded)}")

    # load old dump for comparison
    old_blocks = {}
    if os.path.exists(output_file):
        with open(output_file, 'r', encoding='utf-8') as f:
            old_text = f.read()
        for block in old_text.split('\n\n###############'):
            lines = block.split('\n')
            title_line = None
            after_header = []
            past_header = False
            for line in lines:
                if not past_header:
                    if line.startswith('OBJECT:') or line.startswith('SCRIPT:'):
                        title_line = line.replace(' £CHANGED', '').strip()
                    elif title_line and set(line.strip()) == {'#'}:
                        past_header = True
                else:
                    after_header.append(line)
            if title_line:
                old_blocks[title_line] = '\n'.join(after_header).strip('\n')

    def is_changed(title, new_event_body):
        if title not in old_blocks:
            return True
        return old_blocks[title] != new_event_body

    # collect all events per object (two-pass)
    object_data = OrderedDict()
    for root, dirs, files in os.walk(project_folder):
        for file in files:
            if file.endswith(".gml"):
                script_name = file.replace(".gml", "")
                object_name = os.path.basename(root)
                if script_name not in excluded and object_name not in excluded:
                    filepath = os.path.join(root, file)
                    resource_type = get_resource_type(filepath)
                    with open(filepath, "r", encoding="utf-8") as f:
                        contents = f.read()
                    contents = strip_header(contents)
                    contents = clean_code(contents, remove_part_type=REMOVE_PART_TYPE)
                    if object_name not in object_data:
                        object_data[object_name] = (resource_type, [])
                    object_data[object_name][1].append((script_name, contents))

    scripts_added = 0
    changed_count = 0
    with open(output_file, "w", encoding="utf-8") as dump:
        for object_name, (resource_type, events) in object_data.items():
            event_body = ""
            for script_name, contents in events:
                event_body += "-" * 15 + "\n"
                event_body += "EVENT: " + script_name + "\n"
                event_body += "-" * 15 + "\n"
                event_body += contents + "\n\n"
            event_body = event_body.strip('\n')
            title = resource_type + ": " + object_name
            marker = " £CHANGED" if is_changed(title, event_body) else ""
            if marker:
                changed_count += 1
            dump.write("\n" + "#" * 15 + "\n")
            dump.write(title + marker + "\n")
            dump.write("#" * 15 + "\n\n")
            dump.write(event_body + "\n\n")
            scripts_added += len(events)

    log(f"Done. Scripts added: {scripts_added} | Changed: {changed_count}")
    return output_file


# ─────────────────────────────────────────────
#  INDEX FUNCTIONS
# ─────────────────────────────────────────────

def split_dump(contents, max_objects, max_chars):
    separator = "\n###############"
    entries = [e for e in contents.split(separator) if e.strip()]
    chunks, current_chunk, current_size = [], [], 0
    for entry in entries:
        if len(current_chunk) >= max_objects or current_size + len(entry) > max_chars:
            if current_chunk:
                chunks.append(separator.join(current_chunk))
            current_chunk = [entry]
            current_size = len(entry)
        else:
            current_chunk.append(entry)
            current_size += len(entry)
    if current_chunk:
        chunks.append(separator.join(current_chunk))
    return chunks

def get_changed_blocks(dump):
    changed = []
    for block in dump.split('\n\n###############'):
        if '£CHANGED' in block:
            changed.append(block.replace(' £CHANGED', ''))
    return changed

def delete_from_index(index_path, titles):
    if not os.path.exists(index_path):
        return
    with open(index_path, 'r', encoding='utf-8') as f:
        content = f.read()
    entries = content.split('\n\n')
    kept = [e for e in entries if e.split('\n')[0].strip() not in titles]
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(kept))

def stream_chunk(client, chunk, chunk_num, total_chunks, output_path, mode, log):
    prompt = MESSAGE_TEMPLATE.format(chunk_num=chunk_num, total_chunks=total_chunks) + chunk
    log(f"Chunk {chunk_num}/{total_chunks} — {len(chunk)} chars, ~{len(chunk)//4} tokens...")
    with open(output_path, mode, encoding="utf-8") as f:
        with client.messages.stream(
            model="claude-haiku-4-5",
            system=SYSTEM,
            max_tokens=32000,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text in stream.text_stream:
                f.write(text)
        f.write('\n\n')
    log(f"Chunk {chunk_num} complete. Output tokens: {stream.get_final_message().usage.output_tokens}")

def run_index(output_folder, log, wait_time=61, max_objects=MAX_OBJECTS_PER_CHUNK):
    dump_path  = os.path.join(output_folder, "game_dump.txt")
    index_path = os.path.join(output_folder, "game_index.txt")

    if not os.path.exists(dump_path):
        log("ERROR: game_dump.txt not found. Run Step 1 first.")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        return
    client = Anthropic(api_key=api_key)

    with open(dump_path, 'r', encoding='utf-8') as f:
        dump = f.read()

    changed_blocks = get_changed_blocks(dump)
    if not changed_blocks:
        log("No changes detected — index is up to date.")
        return

    titles = []
    for block in changed_blocks:
        for line in block.strip().split('\n'):
            if line.startswith('OBJECT:') or line.startswith('SCRIPT:'):
                titles.append(line.strip())
                break

    log(f"{len(changed_blocks)} changed block(s) found.")
    for t in titles:
        log(f"  · {t}")

    delete_from_index(index_path, titles)

    changed_dump = '\n\n###############'.join(changed_blocks)
    chunks = split_dump(changed_dump, max_objects, MAX_CHARS_PER_CHUNK)

    for i, chunk in enumerate(chunks):
        stream_chunk(client, chunk, i + 1, len(chunks), index_path, "a", log)
        if i < len(chunks) - 1:
            log(f"Waiting {wait_time} seconds (rate limit)...")
            time.sleep(wait_time)

    log(f"Index updated: {index_path}")


# ─────────────────────────────────────────────
#  EXTRACT FUNCTIONS
# ─────────────────────────────────────────────

def load_dump_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

def get_all_names(dump_text):
    names = []
    for block in dump_text.split('\n\n###############'):
        for line in block.split('\n'):
            line = line.strip()
            if line.startswith('OBJECT:') or line.startswith('SCRIPT:'):
                name = line.replace(' £CHANGED', '').split(':', 1)[1].strip()
                if name:
                    names.append(name)
                break
    return sorted(names)

def get_events_for(dump_text, name):
    for block in dump_text.split('\n\n###############'):
        if f"OBJECT: {name}" in block or f"SCRIPT: {name}" in block:
            return [l.replace('EVENT:', '').strip()
                    for l in block.split('\n') if l.startswith('EVENT:')]
    return []

def find_block(dump_text, name):
    for block in dump_text.split('\n\n###############'):
        if f"OBJECT: {name}" in block or f"SCRIPT: {name}" in block:
            return block
    return None

def find_event(block, event_name):
    for sub in block.split('\n\n---------------'):
        if f"EVENT: {event_name}" in sub:
            return sub
    return None

def is_script_block(block):
    return "SCRIPT:" in block

def strip_block_header(block):
    """Return only the event body of a block, header lines removed."""
    lines = block.split('\n')
    body, past_header, hash_count = [], False, 0
    for line in lines:
        if not past_header:
            s = line.strip()
            if set(s) == {'#'} or s == '':
                hash_count += 1
                if hash_count >= 2:
                    past_header = True
            elif s.startswith('OBJECT:') or s.startswith('SCRIPT:'):
                continue
        else:
            body.append(line)
    return '\n'.join(body).strip('\n')


# ─────────────────────────────────────────────
#  WIN95 STYLE HELPERS
# ─────────────────────────────────────────────

W95_BG    = "#c0c0c0"
W95_DARK  = "#808080"
W95_WHITE = "#ffffff"
W95_BLUE  = "#000080"
W95_BLUE_TXT = "#ffffff"
W95_FONT  = ("Courier New", 9)
W95_FONT_B = ("Courier New", 9, "bold")

def make_button(parent, text, command, width=22):
    return tk.Button(parent, text=text, command=command,
                     font=W95_FONT_B, bg=W95_BG, fg="#000000",
                     activebackground=W95_DARK, activeforeground=W95_WHITE,
                     relief="raised", bd=2, width=width, cursor="hand2")

def make_label(parent, text, bold=False):
    return tk.Label(parent, text=text,
                    font=W95_FONT_B if bold else W95_FONT,
                    bg=W95_BG, fg="#000000")

def make_frame(parent, relief="groove", bd=2):
    return tk.Frame(parent, bg=W95_BG, relief=relief, bd=bd)

def title_bar(parent, title):
    bar = tk.Frame(parent, bg=W95_BLUE)
    bar.pack(fill="x")
    tk.Label(bar, text=title, font=W95_FONT_B, bg=W95_BLUE,
             fg=W95_BLUE_TXT, padx=4, pady=2).pack(side="left")

def folder_row(parent, label_text, var, browse_cmd):
    """A labelled folder picker row."""
    row = tk.Frame(parent, bg=W95_BG)
    row.pack(fill="x", padx=6, pady=2)
    make_label(row, label_text, bold=True).pack(side="left", padx=(4, 6))
    tk.Label(row, textvariable=var, font=W95_FONT, bg=W95_WHITE, fg="#000000",
             relief="sunken", bd=1, width=44, anchor="w").pack(side="left", padx=2)
    make_button(row, "Browse…", browse_cmd, width=9).pack(side="left", padx=4)


# ─────────────────────────────────────────────
#  MAIN APP
# ─────────────────────────────────────────────

class GMLToolsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GML Tools")
        self.root.configure(bg=W95_BG)
        self.root.resizable(False, False)

        # load persisted config
        cfg = load_config()
        self.project_folder = tk.StringVar(value=cfg.get("project_folder", "Not set"))
        self.output_folder  = tk.StringVar(value=cfg.get("output_folder",  "Not set"))
        self.wait_time   = cfg.get("wait_time",   61)
        self.max_objects = cfg.get("max_objects", MAX_OBJECTS_PER_CHUNK)
        self.notes       = cfg.get("notes", "")

        self.dump_text    = None
        self.filter_var   = tk.StringVar()
        self.filter_var.trace_add("write", self._on_filter_change)
        self._basket      = []
        self._current_obj = None
        self._all_names   = []

        self._build_ui()

        # auto-load names if output folder already has a dump
        out = self.output_folder.get()
        if out != "Not set" and os.path.exists(os.path.join(out, "game_dump.txt")):
            self.root.after(100, self._load_names)

    # ── UI construction ──────────────────────

    def _build_ui(self):
        title_bar(self.root, "  GML Tools v1.2  —  GameMaker Project Assistant")

        # folder pickers
        folders = make_frame(self.root)
        folders.pack(fill="x", padx=6, pady=(6, 2))
        folder_row(folders, "SOURCE (GameMaker):", self.project_folder, self._browse_project)
        folder_row(folders, "OUTPUT (dump/index):", self.output_folder,  self._browse_output)

        # main area
        main = tk.Frame(self.root, bg=W95_BG)
        main.pack(fill="both", padx=6, pady=4)

        # ── left panel ──
        left = tk.Frame(main, bg=W95_BG)
        left.pack(side="left", fill="y", padx=(0, 4))

        # actions
        act = make_frame(left)
        act.pack(fill="x", pady=(0, 4))
        title_bar(act, "  Actions")
        btns = tk.Frame(act, bg=W95_BG)
        btns.pack(padx=8, pady=8)
        make_button(btns, "Step 1: Rebuild Dump",  self._run_dump).pack(pady=2)
        make_button(btns, "Step 2: Refresh Index", self._run_index).pack(pady=2)
        make_button(btns, "Step 3: Copy Index",    self._copy_index).pack(pady=2)
        small_row = tk.Frame(btns, bg=W95_BG)
        small_row.pack(pady=(6, 2))
        make_button(small_row, "⚙ Settings", self._open_settings, width=11).pack(side="left", padx=(0, 2))
        make_button(small_row, "✎ Notes",    self._open_notes,    width=11).pack(side="left")
        make_label(btns,
                   "After code changes: Steps 1 then 2.\n"
                   "Step 3 copies index to clipboard.\n"
                   "Paste into your LLM to start session.").pack(pady=(6, 0))

        # extract panel
        ext = make_frame(left)
        ext.pack(fill="both", expand=True)
        title_bar(ext, "  Step 4: Extract Code Blocks")
        inner = tk.Frame(ext, bg=W95_BG)
        inner.pack(fill="both", expand=True, padx=6, pady=6)

        # filter
        fr = tk.Frame(inner, bg=W95_BG)
        fr.pack(fill="x")
        make_label(fr, "Filter:", bold=True).pack(side="left")
        tk.Entry(fr, textvariable=self.filter_var, font=W95_FONT,
                 bg=W95_WHITE, relief="sunken", bd=2, width=30).pack(side="left", padx=4)

        # object list
        make_label(inner, "Objects / Scripts:").pack(anchor="w", pady=(4, 0))
        of = tk.Frame(inner, bg=W95_BG)
        of.pack(fill="both", expand=True)
        os_ = tk.Scrollbar(of)
        os_.pack(side="right", fill="y")
        self.obj_list = tk.Listbox(of, font=W95_FONT, bg=W95_WHITE, fg="#000000",
                                   selectbackground=W95_BLUE, selectforeground=W95_WHITE,
                                   relief="sunken", bd=2, width=34, height=6,
                                   exportselection=0,
                                   yscrollcommand=os_.set)
        self.obj_list.pack(side="left", fill="both", expand=True)
        os_.config(command=self.obj_list.yview)
        self.obj_list.bind("<<ListboxSelect>>", self._on_obj_select)

        # event list
        make_label(inner, "Events — select then Add to Basket:").pack(anchor="w", pady=(4, 0))
        ef = tk.Frame(inner, bg=W95_BG)
        ef.pack(fill="x")
        es = tk.Scrollbar(ef)
        es.pack(side="right", fill="y")
        self.evt_list = tk.Listbox(ef, font=W95_FONT, bg=W95_WHITE, fg="#000000",
                                   selectbackground=W95_BLUE, selectforeground=W95_WHITE,
                                   relief="sunken", bd=2, width=34, height=3,
                                   exportselection=0,
                                   selectmode="multiple", yscrollcommand=es.set)
        self.evt_list.pack(side="left", fill="x", expand=True)
        es.config(command=self.evt_list.yview)
        make_button(inner, "Add Selected to Basket", self._add_to_basket).pack(pady=(3, 0))

        # basket
        make_label(inner, "Basket:", bold=True).pack(anchor="w", pady=(6, 0))
        bf = tk.Frame(inner, bg=W95_BG)
        bf.pack(fill="x")
        bs = tk.Scrollbar(bf)
        bs.pack(side="right", fill="y")
        self.bsk_list = tk.Listbox(bf, font=W95_FONT, bg=W95_WHITE, fg="#000000",
                                   selectbackground=W95_BLUE, selectforeground=W95_WHITE,
                                   relief="sunken", bd=2, width=34, height=3,
                                   yscrollcommand=bs.set)
        self.bsk_list.pack(side="left", fill="x", expand=True)
        bs.config(command=self.bsk_list.yview)

        br = tk.Frame(inner, bg=W95_BG)
        br.pack(fill="x", pady=(4, 0))
        make_button(br, "Copy to Clipboard", self._extract, width=18).pack(side="left")
        make_button(br, "Review", self._review_basket, width=8).pack(side="left", padx=2)
        make_button(br, "Clear", self._clear_basket, width=7).pack(side="left", padx=2)

        # ── right panel — log ──
        lf = make_frame(main)
        lf.pack(side="left", fill="both", expand=True)
        title_bar(lf, "  Log")
        li = tk.Frame(lf, bg=W95_BG)
        li.pack(fill="both", expand=True, padx=4, pady=4)
        ls = tk.Scrollbar(li)
        ls.pack(side="right", fill="y")
        self.log_box = tk.Text(li, font=W95_FONT, bg="#000000", fg="#00ff00",
                               relief="sunken", bd=2, width=46, height=30,
                               state="disabled", yscrollcommand=ls.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        ls.config(command=self.log_box.yview)

        self._log("GML Tools ready.")
        if self.project_folder.get() == "Not set":
            self._log("Set your SOURCE and OUTPUT folders to begin.")
        else:
            self._log(f"Source: {self.project_folder.get()}")
            self._log(f"Output: {self.output_folder.get()}")

    # ── Helpers ──────────────────────────────

    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"> {msg}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        self.root.update_idletasks()

    def _save_config(self):
        save_config({
            "project_folder": self.project_folder.get(),
            "output_folder":  self.output_folder.get(),
            "wait_time":      self.wait_time,
            "max_objects":    self.max_objects,
            "notes":          self.notes,
        })

    def _get_folders(self):
        src = self.project_folder.get()
        out = self.output_folder.get()
        if src == "Not set" or out == "Not set":
            messagebox.showwarning("Folders not set",
                                   "Please set both SOURCE and OUTPUT folders first.")
            return None, None
        return src, out

    def _browse_project(self):
        folder = filedialog.askdirectory(title="Select GameMaker Project Folder")
        if folder:
            self.project_folder.set(folder)
            self._log(f"Source: {folder}")
            self._save_config()

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Select Output Folder (dump + index live here)")
        if folder:
            self.output_folder.set(folder)
            self._log(f"Output: {folder}")
            self._save_config()
            # auto-load if dump already exists
            if os.path.exists(os.path.join(folder, "game_dump.txt")):
                self._load_names()
            else:
                self._log("No dump found in output folder — run Step 1 to build one.")

    def _load_names(self):
        out = self.output_folder.get()
        if out == "Not set":
            return
        dump_path = os.path.join(out, "game_dump.txt")
        if not os.path.exists(dump_path):
            self._log("game_dump.txt not found. Run Step 1 first.")
            return
        self.dump_text = load_dump_file(dump_path)
        self._all_names = get_all_names(self.dump_text)
        self._refresh_obj_list(self._all_names)
        self._log(f"Loaded {len(self._all_names)} objects/scripts.")

    def _refresh_obj_list(self, names):
        self.obj_list.delete(0, "end")
        for name in names:
            self.obj_list.insert("end", name)

    def _on_filter_change(self, *args):
        query = self.filter_var.get().lower()
        filtered = [n for n in self._all_names if query in n.lower()]
        self._refresh_obj_list(filtered)

    def _on_obj_select(self, event):
        if not self.dump_text:
            return
        sel = self.obj_list.curselection()
        if not sel:
            return
        name = self.obj_list.get(sel[0])
        self._current_obj = name
        block = find_block(self.dump_text, name)
        self.evt_list.delete(0, "end")
        if block and is_script_block(block):
            self.evt_list.insert("end", "(script — click Add to Basket)")
        else:
            for evt in get_events_for(self.dump_text, name):
                self.evt_list.insert("end", evt)

    def _add_to_basket(self):
        if not self._current_obj:
            self._log("Select an object or script first.")
            return
        name  = self._current_obj
        block = find_block(self.dump_text, name)
        if not block:
            self._log(f"Block not found: {name}")
            return
        if is_script_block(block):
            entry = (name, None)
            if entry not in self._basket:
                self._basket.append(entry)
                self.bsk_list.insert("end", f"[SCRIPT] {name}")
                self._log(f"Basket: + {name}")
            else:
                self._log(f"Already in basket: {name}")
        else:
            sel = self.evt_list.curselection()
            if not sel:
                self._log("Select at least one event to add.")
                return
            for i in sel:
                evt_name = self.evt_list.get(i)
                entry = (name, evt_name)
                if entry not in self._basket:
                    self._basket.append(entry)
                    self.bsk_list.insert("end", f"{name} › {evt_name}")
                    self._log(f"Basket: + {name} › {evt_name}")
                else:
                    self._log(f"Already in basket: {name} › {evt_name}")

    def _clear_basket(self):
        self._basket = []
        self.bsk_list.delete(0, "end")
        self._log("Basket cleared.")

    def _extract(self):
        if not self._basket:
            self._log("Basket is empty — add some blocks first.")
            return
        if not self.dump_text:
            self._log("No dump loaded.")
            return
        results = []
        current_obj = None
        for name, evt_name in self._basket:
            block = find_block(self.dump_text, name)
            if not block:
                self._log(f"Block not found: {name}")
                continue
            if name != current_obj:
                current_obj = name
                for line in block.split('\n'):
                    s = line.strip()
                    if s.startswith('OBJECT:') or s.startswith('SCRIPT:'):
                        header = s.replace(' £CHANGED', '')
                        results.append(f"{'#' * 15}\n{header}\n{'#' * 15}")
                        break
            if evt_name is None:
                results.append(strip_block_header(block))
            else:
                result = find_event(block, evt_name)
                if result:
                    results.append(result)
                else:
                    self._log(f"Event not found: {name} › {evt_name}")
        if results:
            import pyperclip
            combined = '\n\n'.join(results)
            pyperclip.copy(combined)
            self._log(f"--- {len(results)} block(s) copied ({len(combined)} chars) ---")

    def _review_basket(self):
        """Send basket contents to Opus for code review, stream result to log."""
        if not self._basket:
            self._log("Basket is empty — add some blocks first.")
            return
        if not self.dump_text:
            self._log("No dump loaded.")
            return

        # build the code payload same as extract
        results = []
        current_obj = None
        for name, evt_name in self._basket:
            block = find_block(self.dump_text, name)
            if not block:
                continue
            if name != current_obj:
                current_obj = name
                for line in block.split('\n'):
                    s = line.strip()
                    if s.startswith('OBJECT:') or s.startswith('SCRIPT:'):
                        header = s.replace(' £CHANGED', '')
                        results.append(f"{'#' * 15}\n{header}\n{'#' * 15}")
                        break
            if evt_name is None:
                results.append(strip_block_header(block))
            else:
                result = find_event(block, evt_name)
                if result:
                    results.append(result)

        if not results:
            self._log("Nothing to review.")
            return

        code = '\n\n'.join(results)
        self._log(f"Sending {len(results)} block(s) to Opus for review...")

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._log("ERROR: ANTHROPIC_API_KEY environment variable not set.")
            return

        def task():
            try:
                client = Anthropic(api_key=api_key)
                self._log("--- Opus Review ---")
                with client.messages.stream(
                    model="claude-opus-4-5",
                    max_tokens=1024,
                    messages=[{
                        "role": "user",
                        "content": (
                            "You are an expert GameMaker Studio 2 developer reviewing GML code.\n"
                            "Review the following code. In 400 words or less:\n"
                            "- Highlight any bugs or errors\n"
                            "- Suggest improvements or optimisations for efficiency\n"
                            "- Note anything that looks unusual or risky\n"
                            "Be direct and specific. Reference line content where relevant.\n\n"
                            f"{code}"
                        )
                    }]
                ) as stream:
                    for text in stream.text_stream:
                        self._log_inline(text)
                self._log("\n--- Review complete ---")
            except Exception as e:
                self._log(f"ERROR during review: {e}")

        threading.Thread(target=task, daemon=True).start()

    def _log_inline(self, text):
        """Append text without a newline prefix — for streaming."""
        self.log_box.config(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        self.root.update_idletasks()

    def _copy_index(self):
        _, out = self._get_folders()
        if not out:
            return
        index_path = os.path.join(out, "game_index.txt")
        if not os.path.exists(index_path):
            self._log("game_index.txt not found. Run Steps 1 + 2 first.")
            return
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        import pyperclip
        notes_block = ""
        if self.notes.strip():
            notes_block = f"=== DEVELOPER NOTES ===\n{self.notes.strip()}\n========================\n\n"
        pyperclip.copy(INDEX_PREAMBLE + notes_block + content)
        self._log(f"Index copied to clipboard ({len(content)} chars + preamble).")
        if self.notes.strip():
            self._log("Developer notes included.")
        self._log("Paste into your LLM to start a session.")

    def _run_dump(self):
        src, out = self._get_folders()
        if not src:
            return
        self._log("Building dump...")
        def task():
            try:
                build_dump(src, out, self._log)
                self.dump_text = None
                self._load_names()
            except Exception as e:
                self._log(f"ERROR: {e}")
        threading.Thread(target=task, daemon=True).start()

    def _run_index(self):
        _, out = self._get_folders()
        if not out:
            return
        self._log(f"Running index refresh (wait={self.wait_time}s, max {self.max_objects}/chunk)...")
        def task():
            try:
                run_index(out, self._log, self.wait_time, self.max_objects)
            except Exception as e:
                self._log(f"ERROR: {e}")
        threading.Thread(target=task, daemon=True).start()

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Index Settings")
        win.configure(bg=W95_BG)
        win.resizable(False, False)
        win.grab_set()
        title_bar(win, "  Index Settings")
        frame = make_frame(win)
        frame.pack(padx=10, pady=10, fill="both")
        inner = tk.Frame(frame, bg=W95_BG)
        inner.pack(padx=10, pady=10)

        make_label(inner, "Chunk wait time (seconds):", bold=True).grid(row=0, column=0, sticky="w", pady=4)
        wait_var = tk.StringVar(value=str(self.wait_time))
        tk.Entry(inner, textvariable=wait_var, font=W95_FONT,
                 bg=W95_WHITE, relief="sunken", bd=2, width=8).grid(row=0, column=1, padx=8)
        make_label(inner, "Tier 1: 61s   Tier 2+: 10–20s").grid(row=1, column=0, columnspan=2, sticky="w")

        make_label(inner, "Max objects per chunk:", bold=True).grid(row=2, column=0, sticky="w", pady=(12, 4))
        obj_var = tk.StringVar(value=str(self.max_objects))
        tk.Entry(inner, textvariable=obj_var, font=W95_FONT,
                 bg=W95_WHITE, relief="sunken", bd=2, width=8).grid(row=2, column=1, padx=8)
        make_label(inner, "Default: 25   Reduce if hitting token limits").grid(row=3, column=0, columnspan=2, sticky="w")

        def save():
            try:
                self.wait_time   = int(wait_var.get())
                self.max_objects = int(obj_var.get())
                self._save_config()
                self._log(f"Settings saved: wait={self.wait_time}s, max={self.max_objects}/chunk")
                win.destroy()
            except ValueError:
                messagebox.showerror("Invalid input", "Please enter whole numbers only.")

        br = tk.Frame(inner, bg=W95_BG)
        br.grid(row=4, column=0, columnspan=2, pady=(14, 0))
        make_button(br, "Save",   save,        width=10).pack(side="left", padx=4)
        make_button(br, "Cancel", win.destroy, width=10).pack(side="left", padx=4)

    def _open_notes(self):
        win = tk.Toplevel(self.root)
        win.title("Developer Notes")
        win.configure(bg=W95_BG)
        win.resizable(False, False)
        win.grab_set()
        title_bar(win, "  Developer Notes")

        frame = make_frame(win)
        frame.pack(padx=10, pady=10, fill="both")
        inner = tk.Frame(frame, bg=W95_BG)
        inner.pack(padx=10, pady=10)

        # help tip
        tip = tk.Label(inner,
            text=(
                "These notes are included every time you copy the index (Step 3),\n"
                "so your LLM sees them at the start of every session automatically.\n\n"
                "Use them for things your LLM consistently gets wrong, project-specific\n"
                "conventions, or anything it should always know before you start.\n\n"
                "Example:  draw_rectangle() last argument is 'outline' — True = outline\n"
                "only, False = solid fill. Do not assume."
            ),
            font=W95_FONT, bg="#ffffcc", fg="#000000",
            relief="groove", bd=1, justify="left",
            padx=8, pady=6
        )
        tip.pack(fill="x", pady=(0, 10))

        # text area
        make_label(inner, "Your notes:", bold=True).pack(anchor="w")
        txt_frame = tk.Frame(inner, bg=W95_BG)
        txt_frame.pack(fill="both", expand=True, pady=(4, 0))
        scroll = tk.Scrollbar(txt_frame)
        scroll.pack(side="right", fill="y")
        txt = tk.Text(txt_frame, font=W95_FONT, bg=W95_WHITE, fg="#000000",
                      relief="sunken", bd=2, width=52, height=12,
                      wrap="word", yscrollcommand=scroll.set)
        txt.pack(side="left", fill="both", expand=True)
        scroll.config(command=txt.yview)

        # populate with existing notes
        if self.notes:
            txt.insert("1.0", self.notes)

        def save():
            self.notes = txt.get("1.0", "end-1c")
            self._save_config()
            self._log("Developer notes saved.")
            win.destroy()

        br = tk.Frame(inner, bg=W95_BG)
        br.pack(pady=(10, 0))
        make_button(br, "Save",   save,        width=10).pack(side="left", padx=4)
        make_button(br, "Cancel", win.destroy, width=10).pack(side="left", padx=4)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = GMLToolsApp(root)
    root.mainloop()
