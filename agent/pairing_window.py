"""
Окно первого подключения (tkinter, входит в стандартный Python).

Показывает статус поиска и крупное «число для сверки». Ничего вводить не нужно.
Внизу — запасное поле для адреса Mac на случай, если сеть не пропускает
широковещательный поиск.
"""

from __future__ import annotations

import asyncio
import queue
import threading

from agent.pairing import PairingCancelled, PairingFlow, PairingStatus, parse_manual_address
from agent.state import AgentState


def run_pairing_window(flow: PairingFlow) -> AgentState | None:
    import tkinter as tk

    updates: "queue.Queue[PairingStatus]" = queue.Queue()
    result: dict[str, AgentState | None] = {"state": None}
    flow.on_status = updates.put

    def worker():
        try:
            result["state"] = asyncio.run(flow.run())
        except PairingCancelled:
            pass
        except Exception as e:  # покажем в окне, чтобы было что сфотографировать
            updates.put(PairingStatus("error", f"Ошибка: {e}"))

    root = tk.Tk()
    root.title("Подключение к компьютеру учителя")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.geometry("+200+150")

    frm = tk.Frame(root, padx=28, pady=20)
    frm.pack()
    status = tk.Label(frm, text="Ищу компьютер учителя в сети…", font=("Segoe UI", 13),
                      wraplength=440, justify="center")
    status.pack()
    code_label = tk.Label(frm, text="", font=("Segoe UI", 64, "bold"), fg="#2f6fed")
    code_label.pack(pady=(10, 0))
    hint = tk.Label(frm, text="", font=("Segoe UI", 10), fg="#666", wraplength=440, justify="center")
    hint.pack()

    # Запасной ручной ввод адреса
    manual = tk.Frame(frm, pady=14)
    manual.pack(fill="x")
    tk.Label(manual, text="Не находится? Введите адрес Mac (вверху справа в панели учителя):",
             font=("Segoe UI", 9), fg="#666").pack(anchor="w")
    row = tk.Frame(manual)
    row.pack(fill="x", pady=(4, 0))
    entry = tk.Entry(row, width=22, font=("Consolas", 12))
    entry.pack(side="left")
    err = tk.Label(manual, text="", font=("Segoe UI", 9), fg="#c62828")

    def use_manual(_=None):
        addr = parse_manual_address(entry.get())
        if addr is None:
            err.config(text="Нужны только цифры и точки, например 192.168.3.96")
            err.pack(anchor="w")
            return
        err.pack_forget()
        flow.manual_address = addr
        status.config(text=f"Подключаюсь к {addr[0]}…")

    tk.Button(row, text="Подключиться", command=use_manual).pack(side="left", padx=6)
    entry.bind("<Return>", use_manual)

    def cancel():
        flow.cancelled = True
        root.destroy()

    tk.Button(frm, text="Закрыть", width=12, command=cancel).pack(anchor="e")
    root.protocol("WM_DELETE_WINDOW", cancel)

    def poll():
        try:
            while True:
                st = updates.get_nowait()
                status.config(text=st.text)
                if st.stage == "waiting":
                    code_label.config(text=st.match_code)
                    hint.config(text="Это же число видно в панели учителя в разделе «Новые ноутбуки».")
                    manual.pack_forget()
                elif st.stage == "done":
                    code_label.config(text="✓", fg="#1f9d55")
                    hint.config(text="Агент будет запускаться сам при входе в Windows.")
                    root.after(2500, root.destroy)
                elif st.stage in ("rejected", "error"):
                    code_label.config(text="")
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(200, poll)

    threading.Thread(target=worker, daemon=True).start()
    root.after(200, poll)
    root.mainloop()
    flow.cancelled = True  # окно закрыто — фоновый поиск останавливаем
    return result["state"]
