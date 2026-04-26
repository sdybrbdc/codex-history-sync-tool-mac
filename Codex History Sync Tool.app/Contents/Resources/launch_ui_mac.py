from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


TOOL_ROOT = Path(__file__).resolve().parent
BACKEND_PATH = TOOL_ROOT / "sync_backend.py"


def run_backend(*args: str, codex_home: str | None = None) -> dict:
    cmd = [sys.executable, str(BACKEND_PATH), "--json"]
    if codex_home:
        cmd.extend(["--codex-home", codex_home])
    cmd.extend(args)
    completed = subprocess.run(cmd, capture_output=True, text=True)
    text = (completed.stdout or completed.stderr).strip()
    if not text:
        raise RuntimeError("后端没有返回任何内容。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"后端 JSON 解析失败: {exc}\n\n原始输出:\n{text}") from exc
    if completed.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or text)
    return payload


class MacApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Codex 历史同步工具 (macOS)")
        self.root.geometry("1280x760")
        self.root.minsize(1180, 720)

        self.codex_home_var = tk.StringVar(value=str(Path.home() / ".codex"))
        self.current_status: dict | None = None
        self.backup_map: dict[str, str] = {}
        self.project_map: dict[str, dict] = {}
        self.thread_map: dict[str, dict] = {}
        self.selected_group_key: str | None = None

        self._build_ui()
        self.refresh_status()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        title = ttk.Label(frame, text="Codex 历史同步工具", font=("Helvetica", 18, "bold"))
        title.pack(anchor="w")

        warning = ttk.Label(
            frame,
            text="建议先关闭 Codex Desktop 再执行同步或恢复；mac 版会直接调用同一套后端逻辑。",
        )
        warning.pack(anchor="w", pady=(6, 12))

        path_row = ttk.Frame(frame)
        path_row.pack(fill="x", pady=(0, 10))
        ttk.Label(path_row, text="Codex Home:").pack(side="left")
        ttk.Entry(path_row, textvariable=self.codex_home_var).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(path_row, text="刷新状态", command=self.refresh_status).pack(side="left")

        self.provider_label = ttk.Label(frame, text="当前 provider:")
        self.provider_label.pack(anchor="w")
        self.model_label = ttk.Label(frame, text="当前模型:")
        self.model_label.pack(anchor="w")
        self.summary_label = ttk.Label(frame, text="线程总数:")
        self.summary_label.pack(anchor="w")
        self.projects_label = ttk.Label(frame, text="项目组:")
        self.projects_label.pack(anchor="w")
        self.db_label = ttk.Label(frame, text="数据库:")
        self.db_label.pack(anchor="w", pady=(0, 12))

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(0, 12))
        self.sync_all_button = ttk.Button(
            button_row, text="全量同步到当前", command=self.sync_now
        )
        self.sync_all_button.pack(side="left")
        self.sync_selected_button = ttk.Button(
            button_row,
            text="同步选中会话到当前",
            command=self.sync_selected_threads,
            state="disabled",
        )
        self.sync_selected_button.pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="手动备份", command=self.manual_backup).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="恢复最新备份", command=self.restore_latest).pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="打开备份目录", command=self.open_backup_dir).pack(side="left", padx=(8, 0))

        panes = ttk.Frame(frame)
        panes.pack(fill="both", expand=True)
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=2)
        panes.columnconfigure(2, weight=1)
        panes.rowconfigure(0, weight=1)

        projects_box = ttk.LabelFrame(panes, text="项目分组", padding=8)
        projects_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.projects = ttk.Treeview(
            projects_box,
            columns=("project", "count", "movable", "paths"),
            show="headings",
            height=14,
            selectmode="browse",
        )
        self.projects.heading("project", text="项目")
        self.projects.heading("count", text="线程数")
        self.projects.heading("movable", text="待同步")
        self.projects.heading("paths", text="目录数")
        self.projects.column("project", width=220, anchor="w")
        self.projects.column("count", width=80, anchor="center")
        self.projects.column("movable", width=80, anchor="center")
        self.projects.column("paths", width=80, anchor="center")
        self.projects.pack(fill="both", expand=True)
        self.projects.bind("<<TreeviewSelect>>", self.on_project_selected)

        threads_box = ttk.LabelFrame(panes, text="项目会话", padding=8)
        threads_box.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        self.threads = ttk.Treeview(
            threads_box,
            columns=("title", "provider", "updated", "needs_sync", "cwd"),
            show="headings",
            height=14,
            selectmode="extended",
        )
        self.threads.heading("title", text="会话")
        self.threads.heading("provider", text="Provider")
        self.threads.heading("updated", text="最近更新")
        self.threads.heading("needs_sync", text="待同步")
        self.threads.heading("cwd", text="工作目录")
        self.threads.column("title", width=260, anchor="w")
        self.threads.column("provider", width=110, anchor="center")
        self.threads.column("updated", width=150, anchor="center")
        self.threads.column("needs_sync", width=80, anchor="center")
        self.threads.column("cwd", width=340, anchor="w")
        self.threads.pack(fill="both", expand=True)
        self.threads.bind("<<TreeviewSelect>>", self.on_thread_selection_changed)

        backups_box = ttk.LabelFrame(panes, text="备份列表", padding=8)
        backups_box.grid(row=0, column=2, sticky="nsew")
        self.backup_list = tk.Listbox(backups_box)
        self.backup_list.pack(fill="both", expand=True)
        ttk.Button(backups_box, text="恢复选中备份", command=self.restore_selected).pack(anchor="w", pady=(8, 0))

        log_box = ttk.LabelFrame(frame, text="日志", padding=8)
        log_box.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(log_box, height=10, wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def get_codex_home(self) -> str:
        return self.codex_home_var.get().strip()

    def refresh_status(self) -> None:
        try:
            payload = run_backend("status", codex_home=self.get_codex_home())
            projects_payload = run_backend("projects", codex_home=self.get_codex_home())
        except Exception as exc:
            messagebox.showerror("刷新失败", str(exc))
            self.append_log(f"刷新失败: {exc}")
            return

        self.current_status = payload
        self.provider_label.config(text=f"当前 provider: {payload['current_provider']}")
        self.model_label.config(text=f"当前模型: {payload.get('current_model') or '未读取到'}")
        rollout_mismatches = int(payload.get("rollout_metadata_mismatches") or 0)
        projects = projects_payload.get("projects") or []
        self.summary_label.config(
            text=(
                f"线程总数: {payload['total_threads']}    "
                f"可同步线程: {payload['movable_threads']}    "
                f"Rollout 元数据待同步: {rollout_mismatches}"
            )
        )
        self.projects_label.config(text=f"项目组: {len(projects)}")
        self.db_label.config(text=f"数据库: {payload['db_path']}")

        self.backup_list.delete(0, "end")
        self.backup_map = {}
        for backup in payload["backups"]:
            label = f"{backup['modified_at']}    {backup['name']}"
            self.backup_map[label] = backup["path"]
            self.backup_list.insert("end", label)

        previous_group_key = self.selected_group_key
        self.project_map = {
            str(project["group_key"]): project for project in projects
        }
        for item in self.projects.get_children():
            self.projects.delete(item)
        for project in projects:
            iid = str(project["group_key"])
            self.projects.insert(
                "",
                "end",
                iid=iid,
                values=(
                    project["label"],
                    project["thread_count"],
                    project["movable_threads"],
                    project["cwd_variants"],
                ),
            )

        if previous_group_key and previous_group_key in self.project_map:
            self.selected_group_key = previous_group_key
            self.projects.selection_set(previous_group_key)
            self.projects.focus(previous_group_key)
        elif projects:
            self.selected_group_key = str(projects[0]["group_key"])
            self.projects.selection_set(self.selected_group_key)
            self.projects.focus(self.selected_group_key)
        else:
            self.selected_group_key = None

        self.refresh_thread_list()
        self.update_sync_selected_state()

        self.append_log(
            f"状态已刷新。当前 provider={payload['current_provider']}，"
            f"可同步线程={payload['movable_threads']}，"
            f"Rollout 元数据待同步={rollout_mismatches}，"
            f"项目组={len(projects)}。"
        )

    def on_project_selected(self, _event: object | None = None) -> None:
        selection = self.projects.selection()
        self.selected_group_key = selection[0] if selection else None
        self.refresh_thread_list()
        self.update_sync_selected_state()

    def refresh_thread_list(self) -> None:
        for item in self.threads.get_children():
            self.threads.delete(item)
        self.thread_map = {}

        if not self.selected_group_key:
            return

        try:
            payload = run_backend(
                "threads",
                "--group-key",
                self.selected_group_key,
                codex_home=self.get_codex_home(),
            )
        except Exception as exc:
            messagebox.showerror("读取会话失败", str(exc))
            self.append_log(f"读取会话失败: {exc}")
            return

        threads = payload.get("threads") or []
        self.thread_map = {str(thread["id"]): thread for thread in threads}
        for thread in threads:
            iid = str(thread["id"])
            self.threads.insert(
                "",
                "end",
                iid=iid,
                values=(
                    thread["title"],
                    thread["model_provider"],
                    thread["updated_at"],
                    "是" if thread["needs_sync"] else "",
                    thread["cwd"],
                ),
            )

    def on_thread_selection_changed(self, _event: object | None = None) -> None:
        self.update_sync_selected_state()

    def update_sync_selected_state(self) -> None:
        state = "normal" if self.threads.selection() else "disabled"
        self.sync_selected_button.configure(state=state)

    def sync_now(self) -> None:
        if not self.current_status:
            self.refresh_status()
        movable_threads = int((self.current_status or {}).get("movable_threads") or 0)
        rollout_mismatches = int((self.current_status or {}).get("rollout_metadata_mismatches") or 0)
        if self.current_status and movable_threads <= 0 and rollout_mismatches <= 0:
            messagebox.showinfo("无需同步", "当前已经没有需要迁移到当前 provider 的线程。")
            self.append_log("同步跳过：没有需要迁移的线程。")
            return
        if not messagebox.askokcancel(
            "确认同步",
            "将其他 provider 的线程和 rollout 元数据统一归到当前 provider，且会先自动备份。",
        ):
            self.append_log("用户取消了同步。")
            return
        try:
            payload = run_backend("sync", codex_home=self.get_codex_home())
            self.append_log(f"同步完成。已移动 {payload['updated_rows']} 条线程。")
            rollout_metadata = payload.get("rollout_metadata") or {}
            if rollout_metadata:
                self.append_log(
                    f"Rollout 元数据已更新 {rollout_metadata.get('updated_files', 0)} 个文件。"
                )
            self.append_log(f"备份文件: {payload['backup_path']}")
            session_meta_backup = payload.get("session_meta_backup") or {}
            if session_meta_backup.get("path"):
                self.append_log(f"Rollout 元数据备份: {session_meta_backup['path']}")
            self.refresh_status()
            remaining_threads = int(payload.get("remaining_threads") or 0)
            remaining_rollout_metadata = int(payload.get("remaining_rollout_metadata") or 0)
            if payload.get("verified") and remaining_threads == 0 and remaining_rollout_metadata == 0:
                messagebox.showinfo("同步完成", "同步完成。若历史列表没有立刻刷新，重开一次 Codex 即可。")
            else:
                messagebox.showwarning(
                    "同步未完全完成",
                    (
                        f"同步后仍有 {remaining_threads} 条线程、"
                        f"{remaining_rollout_metadata} 个 rollout 元数据不在当前 provider。"
                        "建议关闭 Codex 后再同步一次。"
                    ),
                )
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc))
            self.append_log(f"同步失败: {exc}")

    def sync_selected_threads(self) -> None:
        selection = list(self.threads.selection())
        if not selection:
            messagebox.showwarning("未选择会话", "先在中间列表选择至少一个会话。")
            return

        project = self.project_map.get(self.selected_group_key or "", {})
        project_label = project.get("label") or "当前项目"
        if not messagebox.askokcancel(
            "确认同步选中会话",
            f"将项目【{project_label}】中选中的 {len(selection)} 个会话同步到当前 provider，并先自动备份数据库与 rollout 元数据。",
        ):
            self.append_log("用户取消了选中会话同步。")
            return

        try:
            args = ["sync-selected"]
            for thread_id in selection:
                args.extend(["--thread-id", thread_id])
            payload = run_backend(*args, codex_home=self.get_codex_home())
            self.append_log(
                f"选中同步完成。项目={project_label}，选中 {payload['selected_count']} 条，会话 provider 更新 {payload['updated_rows']} 条。"
            )
            rollout_metadata = payload.get("rollout_metadata") or {}
            self.append_log(
                f"Rollout 元数据已更新 {rollout_metadata.get('updated_files', 0)} 个文件。"
            )
            self.append_log(f"备份文件: {payload['backup_path']}")
            session_meta_backup = payload.get("session_meta_backup") or {}
            if session_meta_backup.get("path"):
                self.append_log(f"Rollout 元数据备份: {session_meta_backup['path']}")
            self.refresh_status()
            if payload.get("verified"):
                messagebox.showinfo(
                    "同步完成",
                    f"已完成 {payload['selected_count']} 个会话的定向同步。若历史列表没有立刻刷新，重开一次 Codex 即可。",
                )
            else:
                messagebox.showwarning(
                    "同步未完全完成",
                    (
                        f"选中会话里仍有 {payload['remaining_selected_provider_mismatches']} 条 provider 不一致，"
                        f"{payload['remaining_selected_rollout_mismatches']} 个 rollout 元数据不一致。"
                    ),
                )
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc))
            self.append_log(f"选中会话同步失败: {exc}")

    def manual_backup(self) -> None:
        try:
            payload = run_backend("backup", codex_home=self.get_codex_home())
            self.append_log(f"手动备份完成: {payload['backup_path']}")
            session_meta_backup = payload.get("session_meta_backup") or {}
            if session_meta_backup.get("path"):
                self.append_log(f"Rollout 元数据备份: {session_meta_backup['path']}")
            self.refresh_status()
        except Exception as exc:
            messagebox.showerror("备份失败", str(exc))
            self.append_log(f"备份失败: {exc}")

    def restore_latest(self) -> None:
        if not messagebox.askokcancel("确认恢复", "将恢复最新备份，并在恢复前自动创建安全备份。"):
            self.append_log("用户取消了恢复最新备份。")
            return
        try:
            payload = run_backend("restore", codex_home=self.get_codex_home())
            self.append_log(f"已恢复最新备份: {payload['restored_from']}")
            self.append_log(f"恢复前安全备份: {payload['safety_backup']}")
            restored_session_meta = payload.get("restored_session_meta") or {}
            if restored_session_meta.get("path"):
                self.append_log(
                    f"已恢复 Rollout 元数据 {restored_session_meta.get('restored_files', 0)} 个文件。"
                )
            self.refresh_status()
            messagebox.showinfo("恢复完成", "恢复完成。建议重开一次 Codex 再看历史列表。")
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))
            self.append_log(f"恢复失败: {exc}")

    def restore_selected(self) -> None:
        selection = self.backup_list.curselection()
        if not selection:
            messagebox.showwarning("未选择备份", "先在右侧选一个备份。")
            return
        label = self.backup_list.get(selection[0])
        backup_path = self.backup_map.get(label)
        if not backup_path:
            messagebox.showerror("恢复失败", "无法解析选中的备份路径。")
            return
        if not messagebox.askokcancel("确认恢复", f"将恢复这个备份：\n{backup_path}\n\n恢复前会先自动生成一份安全备份。"):
            self.append_log("用户取消了恢复。")
            return
        try:
            payload = run_backend("restore", "--backup", backup_path, codex_home=self.get_codex_home())
            self.append_log(f"恢复完成。来源备份: {payload['restored_from']}")
            self.append_log(f"恢复前安全备份: {payload['safety_backup']}")
            restored_session_meta = payload.get("restored_session_meta") or {}
            if restored_session_meta.get("path"):
                self.append_log(
                    f"已恢复 Rollout 元数据 {restored_session_meta.get('restored_files', 0)} 个文件。"
                )
            self.refresh_status()
            messagebox.showinfo("恢复完成", "恢复完成。建议重开一次 Codex 再看历史列表。")
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))
            self.append_log(f"恢复失败: {exc}")

    def open_backup_dir(self) -> None:
        if not self.current_status:
            self.refresh_status()
        backup_dir = self.current_status.get("backup_dir") if self.current_status else None
        if not backup_dir:
            messagebox.showerror("打开失败", "还没有读取到备份目录。")
            return
        path = Path(backup_dir)
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(path)], check=False)
        self.append_log(f"已打开备份目录: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="macOS GUI for Codex history sync tool")
    parser.add_argument("--smoke-test", action="store_true", help="Run a backend connectivity check and exit")
    parser.add_argument("--codex-home", help="Override Codex home directory for smoke testing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.smoke_test:
        payload = run_backend("status", codex_home=args.codex_home)
        print(
            f"Smoke test OK: provider={payload['current_provider']} "
            f"movable_threads={payload['movable_threads']}"
        )
        return 0

    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("aqua")
    except tk.TclError:
        pass
    MacApp(root)

    # Try hard to bring the window to the foreground on macOS launch.
    root.update_idletasks()
    root.deiconify()
    root.lift()
    try:
        root.focus_force()
    except tk.TclError:
        pass
    try:
        root.attributes("-topmost", True)
        root.after(250, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to set frontmost of the first process whose unix id is '
                f'{os.getpid()} to true',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
