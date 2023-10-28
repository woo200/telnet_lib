# --------------------------------------------------------------------------------
# Copyright (c) 2023 John Woo
#
# TelServ (TelnetServer)
# 
# This file is part of the TelServ project. For full license information, see the
# LICENSE.md file at the root of the source code directory
# --------------------------------------------------------------------------------

import socket
import threading
import traceback
import time 
import io
import struct

from telserv import __version__, TelnetCommands, TelnetOptions, DumbTerminal, Client, ClientOptions
from dataclasses import dataclass
from enum import Enum

class TelnetServer:
    def __init__(self, **kwargs):
        default = {
            "host": "127.0.0.1",
            "port": 23,
            "ipv6": False,
            "client_handler": ClientHandler
        }
        self.args = {**default, **kwargs}
        self.sock = socket.socket(socket.AF_INET if not self.args['ipv6'] else socket.AF_INET, 
                                  socket.SOCK_STREAM)

    def run(self):
        """ 
        Run the server forever
        """
        self.sock.bind((self.args['host'], self.args['port']))
        self.sock.listen(1)

        self.client_pool = []
        self.stop_signal = threading.Event()
        
        self.server_thread = threading.Thread(
            target=self.__run_forever,
            daemon=True
        )
        self.server_thread.start()

        ServerConsole(self).console()
    
    def stop(self):
        """
        Stop the server
        """
        if self.stop_signal.is_set():
            return
        self.stop_signal.set()

        for client in self.client_pool:
            client.hard_kill_signal.set()
            client.client_thread.join()
        self.sock.close()

    def __clean_dead_threads(self):
        """
        Clean up dead threads
        """
        for client in self.client_pool:
            if not client.client_thread.is_alive() or client.hard_kill_signal.is_set():
                self.client_pool.remove(client)

    def __run_forever(self):
        zombie_timer = time.time()
        try:
            while not self.stop_signal.is_set():
                if time.time() - zombie_timer > 5:
                    self.__clean_dead_threads()
                    zombie_timer = time.time()

                # Accept new connections
                self.sock.settimeout(10)
                try:
                    conn, addr = self.sock.accept()
                except socket.timeout:
                    continue

                hard_kill_signal = threading.Event()
                handler = self.args["client_handler"](conn, addr, hard_kill_signal)
                client_thread = handler.run_threaded()
                client_connection = ClientConnection(conn, addr, hard_kill_signal, client_thread, handler)

                self.client_pool.append(client_connection)
        except:
            traceback.print_exc() # Print stack trace on error
        self.stop() # Gracefully stop the server

@dataclass
class ClientConnection:
    conn: socket.socket
    addr: tuple
    hard_kill_signal: threading.Event
    client_thread: threading.Thread
    handler: object

    def __repr__(self) -> str:
        return f"<ClientConnection {self.addr}>"

class ServerConsole:
    def __init__(self, server):
        self.server = server

    def print_console(self, message):
        print(f"\r{message}\n> ", end="")

    def console(self):
        self.print_console(f"TelServ v{__version__} Console")
        try:
            while True:
                cmd = input()
                args = cmd.split(" ")
                if args[0] == 'clients':
                    buf = "Connected clients:\n"
                    for i,client in enumerate(self.server.client_pool):
                        ip, port = client.handler.addr
                        buf += f"    {i} - {ip}\n"
                    self.print_console(buf)
                elif args[0] == 'stop':
                    self.print_console("Stopping server...")
                    self.server.stop()
                    self.print_console("Server stopped")
                    break
                elif args[0] == 'iac':
                    client = args[1]
                    cmd = TelnetCommands[args[2].upper()] # DO, DONT
                    opt = TelnetOptions[args[3].upper()]
                    client = self.server.client_pool[int(client)]
                    data = client.handler.options.send_one(cmd, opt)
                    client.conn.sendall(data)
                    hex_str = ':'.join(hex(x)[2:] for x in data)
                    self.print_console(f"Sent {cmd.name} {opt.name} [{hex_str}] to {client}")
                elif args[0] == 'opts':
                    client = args[1]
                    client = self.server.client_pool[int(client)]
                    self.print_console(f"Client Options: {client.handler.options}")
                elif args[0] == 'kick':
                    client = args[1]
                    client = self.server.client_pool[int(client)]
                    self.print_console(f"Kicking client {client}")
                    client.hard_kill_signal.set()
                    client.client_thread.join()
                    self.server.client_pool.remove(client)
                    self.print_console(f"Client {client} kicked")
                else:
                    self.print_console(f"Unknown command: {cmd}")
        except KeyboardInterrupt:
            print("\rKeyboard interrupt detected, stopping server...")
            self.server.stop()
            print("Server stopped")

class ClientState(Enum):
    CONNECT = 0
    UAUTH = 1
    AUTH = 2

class ClientHandler:
    def __init__(self, conn, addr, signal):
        self.conn = conn
        self.addr = addr
        self.signal = signal
        self.options = ClientOptions()
        self.dumbterm = DumbTerminal(conn, self.options)
        self.state: ClientState = ClientState.CONNECT

        self.events = {}

        self.client = None

    def run_threaded(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run,
            daemon=True
        )
        watchdog_thread = threading.Thread(
            target=self.watchdog,
            daemon=True
        )
        thread.start()
        watchdog_thread.start()
        return thread

    def watchdog(self):
        while not self.signal.is_set():
            time.sleep(1)
        self.conn.close()

    def __process_opts(self, data: bytes) -> bool:
        if data[0] != TelnetCommands.IAC.value:
            return False
        res = self.options.iac(data)
        if res:
            self.conn.sendall(res)
        return True

    def on(self, event, callback):
        self.events[event] = callback
    
    def __dispatch_event(self, event, *args, **kwargs):
        if event in self.events:
            self.events[event](*args, **kwargs)

    def close(self):
        self.signal.set()

    def run(self):
        # Send initial options
        self.conn.settimeout(1)
        try:
            self.__process_opts(self.conn.recv(1024)) # Process initial options (If there are any)
        except socket.timeout:
            pass
        self.conn.sendall(self.options.send(
            (TelnetCommands.WILL, TelnetOptions.ECHO),              # Please disable local echo
            (TelnetCommands.WILL, TelnetOptions.SUPPRESS_GO_AHEAD), # Please suppress go ahead
            (TelnetCommands.DO, TelnetOptions.NEGOTIATE_ABOUT_WINDOW_SIZE),     # Please send terminal type
        ))

        self.client = Client(self)
        self.__dispatch_event("connect")

        while not self.signal.is_set():
            self.conn.settimeout(1)
            try:
                data = self.conn.recv(1024)
                if not data:
                    break
                if self.__process_opts(data):
                    continue

                self.__dispatch_event("data", data)
            except socket.timeout:
                continue
            except OSError: 
                break
        self.signal.set()
        self.conn.close()
        