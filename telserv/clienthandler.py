# --------------------------------------------------------------------------------
# Copyright (c) 2023 John Woo
#
# TelServ (TelnetServer)
# 
# This file is part of the TelServ project. For full license information, see the
# LICENSE.md file at the root of the source code directory
# --------------------------------------------------------------------------------

from .telnet_860 import TelnetOptions, TelnetCommands

import time
import struct
import io

class ClientOptions:
    def __init__(self, options={}) -> None:
        self.opts = {
            "window_size": (0,0),
            **options
        }
        self.event_handlers = {}

    def on(self, opt: TelnetOptions, func):
        if opt not in self.event_handlers:
            self.event_handlers[opt] = [func]
        else:
            self.event_handlers[opt].append(func)
    
    def __dispatch_event(self, opt: TelnetOptions):
        if opt not in self.event_handlers:
            return
        for func in self.event_handlers[opt]:
            func()
    
    def iac_sb(self, data: io.BytesIO) -> bytes:
        opt = data.read(1)
        try:
            opt = TelnetOptions(opt[0])
        except ValueError:
            d = data.read(1)
            while d[0] != TelnetCommands.SE.value:
                d = data.read(1)
            return b""
        
        if opt == TelnetOptions.NEGOTIATE_ABOUT_WINDOW_SIZE:
            w,h = struct.unpack(">HH", data.read(4))
            self.opts["window_size"] = (w, h)
            data.read(2) # Skip IAC SE
            self.__dispatch_event(opt) # Dispatch event (after processing)
            return b""
        
        # Skip until IAC SE
        d = data.read(1)
        while d[0] != TelnetCommands.SE.value:
            d = data.read(1)
        self.__dispatch_event(opt) # Dispatch event
        return b""

    def iac(self, data: bytes) -> bytes:
        data = io.BytesIO(data)
        ret = b""
        while True:
            iac = data.read(1)
            if not iac or iac[0] != TelnetCommands.IAC.value:
                break
            cmd = data.read(1)
            try:
                cmd = TelnetCommands(cmd[0])
            except ValueError:
                continue

            if cmd in (TelnetCommands.DO, 
                       TelnetCommands.DONT, 
                       TelnetCommands.WILL, 
                       TelnetCommands.WONT):
                # Read option
                opt = data.read(1)
                try:
                    opt = TelnetOptions(opt[0])
                except ValueError:
                    continue
                # Process option
                ret += self.iac_one(cmd, opt)
                self.__dispatch_event(opt) # Dispatch event
                continue
            elif cmd == TelnetCommands.SB:
                ret += self.iac_sb(data)
                continue
        return ret

    def iac_one(self, cmd: TelnetCommands, opt: TelnetOptions) -> bytes:
        if cmd == TelnetCommands.DO:
            self.opts[opt] = True
        elif cmd == TelnetCommands.DONT:
            self.opts[opt] = False
        elif cmd == TelnetCommands.WILL:
            self.opts[opt] = True
            return bytes([TelnetCommands.IAC.value, TelnetCommands.DO.value, opt.value])
        elif cmd == TelnetCommands.WONT:
            self.opts[opt] = False
            return bytes([TelnetCommands.IAC.value, TelnetCommands.DONT.value, opt.value])
        return b""
    
    def send_one(self, cmd: TelnetCommands, opt: TelnetOptions) -> bytes:
        return bytes([TelnetCommands.IAC.value, cmd.value, opt.value])

    def send(self, *opts) -> bytes:
        data = b""
        for opt in opts:
            data += self.send_one(*opt)
        return data
    
    def send_all_opts(self) -> bytes:
        data = b""
        for opt in self.opts:
            data += self.send_one(TelnetCommands.DO, opt)
        return data

    def __repr__(self) -> str:
        data = "<ClientOptions>\n"
        for opt, val in self.opts.items():
            if isinstance(opt, TelnetOptions):
                data += f"    I {'WILL' if val else 'WONT'} {opt.name}\n"
            else:
                data += f"    {opt}: {val}\n"
        return data

    def get(self, opt):
        return self.opts[opt]

class DumbTerminal:
    def __init__(self, conn, options: ClientOptions) -> None:
        self.conn = conn
        self.options: ClientOptions = options

    def send_raw(self, data: bytes):
        self.conn.send(data)
    
    def set_cursor(self, x: int, y: int):
        self.send_raw(b'\x1b[%d;%dH' % (y, x))
    
    def clear_screen(self):
        self.send_raw(b'\x1b[2J')
    
    def cursor_blink(self, enabled: bool):
        self.send_raw(b'\x1b[?25h' if enabled else b'\x1b[?25l')

    def print_centered(self, data: str, line_y: int, **kwargs):
        args = {
            "end": "",
            **kwargs
        }

        w, h = self.options.get("window_size")
        self.set_cursor((w - len(data)) // 2, line_y)
        self.print(data, **args)
    
    def print(self, data: str, *args, **kwargs):
        optargs = {
            "end": "\r\n",
            **kwargs
        }
        if len(args) == 2:
            x, y = args
            self.set_cursor(x, y)

        self.send_raw(f"{data}{optargs['end']}".encode('utf-8'))
    
    def input(self, **kwargs):
        optargs = {
            "replace": None,
            **kwargs
        }
        strbuild = b""
        while True:
            data = self.conn.recv(1)
            if not data:
                break
            strbuild += data
            if data == b'\r':
                break

class RenderableElement:
    def __init__(self):
        self.enabled = True

    def draw(self, term: DumbTerminal):
        raise NotImplementedError()
    
    def get_position(self):
        raise NotImplementedError()

    def set_position(self, x: int, y: int):
        raise NotImplementedError()

    def set_enabled(self, enabled: bool):
        self.enabled = enabled

class ClientRenderer:
    PRERENDER = 0
    POSTRENDER = 1

    def __init__(self, ch) -> None:
        self.ch = ch
        self.dumbterm = ch.dumbterm

        self.pre_render_tasks = []
        self.post_render_tasks = []
        self.screen = None

        self.ch.options.on(TelnetOptions.NEGOTIATE_ABOUT_WINDOW_SIZE, self.render)
    
    def on(self, event: int, func):
        if event == self.PRERENDER:
            self.pre_render_tasks.append(func)
        elif event == self.POSTRENDER:
            self.post_render_tasks.append(func)
    
    def dispatch_event(self, event: int):
        if event == self.PRERENDER:
            for func in self.pre_render_tasks:
                func()
        elif event == self.POSTRENDER:
            for func in self.post_render_tasks:
                func()
    
    def set_screen(self, screen):
        if self.screen:
            self.screen.enabled = False
        self.screen = screen
        if self.screen:
            self.screen.enabled = True

        self.render()
    
    def render(self):
        self.dispatch_event(self.PRERENDER)
        if not self.screen:
            return
        
        self.dumbterm.clear_screen()
        for element in self.screen.elements:
            if element.enabled:
                element.draw(self.dumbterm)
        self.dispatch_event(self.POSTRENDER)

class TextElement(RenderableElement):
    def __init__(self, text: str, **kwargs) -> None:
        super().__init__()
        
        self.text = text
        self.args = {
            "x": 0,
            "y": 0,
            "offset_x": 0,
            "offset_y": 0,
            "centered_y": False,
            "centered_x": False,
            **kwargs
        }

    def get_position(self, term: DumbTerminal):
        w, h = term.options.get("window_size")
        x, y = self.args["x"], self.args["y"]

        if self.args["x"] == -1:
            x = w - len(self.text)
        if self.args["y"] == -1:
            y = h - len(self.text)

        if self.args["centered_y"]:
            y = (h // 2) + self.args["offset_y"]
        if self.args["centered_x"]:
            x = (w - len(self.text)) // 2 + self.args["offset_x"]
        return (x, y)
    
    def set_position(self, x: int, y: int):
        self.args["x"] = x
        self.args["y"] = y

    def draw(self, term: DumbTerminal):
        x, y = self.get_position(term)
        term.print(self.text, x, y)

class InputElement(RenderableElement):
    def __init__(self, **kwargs) -> None:
        super().__init__()
        
        self.args = {
            "x": 0,
            "y": 0,
            "override_char": None,
            **kwargs
        }
        self.input_done = False
        self.current_text = b""
    
    def get_position(self):
        return (self.args["x"], self.args["y"])

    def set_position(self, x: int, y: int):
        self.args["x"] = x
        self.args["y"] = y

    def draw(self, term: DumbTerminal):
        if self.args["override_char"]:
            term.print(self.args["override_char"] * len(self.current_text), self.args["x"], self.args["y"], end="")
        else:
            term.print(self.current_text.decode(), self.args["x"], self.args["y"], end="")

class Screen:
    def __init__(self, client) -> None:
        self.elements = []
        self.enabled = False
        self.client = client

    def add_element(self, element: RenderableElement):
        self.elements.append(element)

    def remove_element(self, element: RenderableElement):
        self.elements.remove(element)

class StudentSearchScreen(Screen):
    SEARCH = 0
    RESULTS = 1

    def __init__(self, client) -> None:
        super().__init__(client)

        self.__setup_elements()
        self.__setup_events()

        self.cursor_idx = 0
        self.cursor_positions = []

        self.grades = [
            ['S-202', 'BIOLOGY 2', 'F', 'LIGGET', '3', '214'],
            ['E-314', 'ENGLISH 11B', 'D', 'TURMAN', '5', '172'],
            ['H-221', 'WORLD HISTORY 11B', 'C', 'DWYER', '2', '108'],
            ['M-106', 'TRIG 2', 'B', 'DICKERSON', '4', '315'],
            ['PE-02', "PHYSICAL EDUCATION", 'C', "COMSTOCK", "1", "GYM"],
            ['M-122', "CALCULUS 1", "B", "LOGAN", "6", "240"]
        ]
        self.grade_elements = []

        self.state = StudentSearchScreen.SEARCH

    def __setup_elements(self):
        self.te1 = TextElement("PLEASE ENTER STUDENT NAME: ", x=0, y=1)
        self.input1 = InputElement(x=len(self.te1.text), y=1)

        self.add_element(self.te1)
        self.add_element(self.input1)
    
    def __setup_results(self):
        for element in self.grade_elements:
            self.remove_element(element)

        if len(self.grade_elements) == 0:
            self.te2 = TextElement("CLASS #   COURSE TITLE         GRADE   TEACHER   PERIOD   ROOM", x=0, y=3, centered_x=True)
            self.te3 = TextElement("─" * len(self.te2.text), x=0, y=4, centered_x=True)
            self.te4 = TextElement("TO CHANGE ANY ITEM, MOVE CURSOR TO DESIRED POSITION AND ENTER NEW VALUE", x=0, y=6+len(self.grades))

            self.add_element(self.te2)
            self.add_element(self.te3)
            self.add_element(self.te4)

        self.grade_elements = []

        for i, grade in enumerate(self.grades):
            class_num, title, grade, teacher, period, room = grade

            s1 = " " * (len("CLASS #   ") - len(class_num))
            s2 = " " * (len("COURSE TITLE         ") - len(title))
            s3 = " " * (len("GRADE   ") - len(grade))
            s4 = " " * (len("TEACHER   ") - len(teacher))
            s5 = " " * (len("PERIOD   ") - len(period))

            text = f"{class_num}{s1}{title}{s2}{grade}{s3}{teacher}{s4}{period}{s5}{room}"
            element = TextElement(text, x=0, y=5+i, centered_x=True)

            self.grade_elements.append(element)
            
        for element in self.grade_elements:
            self.add_element(element)

    def __handle_input_results(self, data: bytes):
        if data in (b'\x1b[C', b'\x1b[A'):
            self.cursor_idx += 1
            if self.cursor_idx >= len(self.cursor_positions):
                self.cursor_idx = 0
        elif data in (b'\x1b[D', b'\x1b[B'):
            self.cursor_idx -= 1
            if self.cursor_idx < 0:
                self.cursor_idx = len(self.cursor_positions) - 1
        else:
            if data == b'\x1b':
                self.client.dumbterm.clear_screen()
                self.client.close()
                return
            if self.cursor_idx%2 == 0:
                self.grades[self.cursor_idx//2][2] = data.decode()
            else:
                self.grades[self.cursor_idx//2][4] = data.decode()

        self.__setup_results()
        self.client.renderer.render()
        
    def __handle_input_search(self, data: bytes):
        if data.endswith(b'\r\x00'):
            self.input1.input_done = True
            self.state = StudentSearchScreen.RESULTS
            self.__setup_results()
        elif data == b'\x7f':
            self.input1.current_text = self.input1.current_text[:-1]
        elif not self.input1.input_done and len(data) == 1:
            self.input1.current_text += data
        self.client.renderer.render()

    def __calculate_cursor_positions(self):
        self.cursor_positions = []
        for element in self.grade_elements:
            base_x, base_y = element.get_position(self.client.dumbterm)

            s1 = len("CLASS #   ")
            s2 = len("COURSE TITLE         ")
            s3 = len("GRADE   ")
            s4 = len("TEACHER   ")
            s5 = len("PERIOD   ")

            # self.cursor_positions.append((base_x, base_y))
            # self.cursor_positions.append((base_x + s1, base_y))
            self.cursor_positions.append((base_x + s1 + s2, base_y))
            # self.cursor_positions.append((base_x + s1 + s2 + s3, base_y))
            self.cursor_positions.append((base_x + s1 + s2 + s3 + s4, base_y))
            # self.cursor_positions.append((base_x + s1 + s2 + s3 + s4 + s5, base_y))

    def __handle_prerender(self):
        if not self.enabled:
            return
        self.client.dumbterm.cursor_blink(False)

    def __handle_postrender(self):
        if not self.enabled:
            return
        
        self.client.dumbterm.cursor_blink(True)

        if self.state == StudentSearchScreen.RESULTS:
            self.__calculate_cursor_positions()
            self.client.dumbterm.set_cursor(*self.cursor_positions[self.cursor_idx])

    def __handle_input(self, data: bytes):
        if not self.enabled:
            return
        if self.state == StudentSearchScreen.SEARCH:
            self.__handle_input_search(data)
        elif self.state == StudentSearchScreen.RESULTS:
            self.__handle_input_results(data)

    def __setup_events(self):
        self.client.ch.on("data", self.__handle_input)
        self.client.renderer.on(ClientRenderer.POSTRENDER, self.__handle_postrender)
        self.client.renderer.on(ClientRenderer.PRERENDER, self.__handle_prerender)

class LoginScreen(Screen):
    def __init__(self, client) -> None:
        super().__init__(client)

        self.__setup_elements()
        self.__setup_events()

    def __setup_elements(self):
        self.te1 = TextElement("PDP 11/270 PRB TIP # 45", x=0, y=1)
        self.te2 = TextElement("WELCOME TO THE SEATTLE PUBLIC SCHOOL DISTRICT DATANET", x=0, y=2)
        self.te3 = TextElement("PLEASE LOGON WITH USER PASSWORD:  ", x=0, y=4)
        self.te4 = TextElement("TTY 34/984", x=-1, y=1)

        self.input1 = InputElement(x=len(self.te3.text), y=4)

        self.add_element(self.te1)
        self.add_element(self.te2)
        self.add_element(self.te3)
        self.add_element(self.te4)
        self.add_element(self.input1)
    
    def __handle_input(self, data: bytes):
        if not self.enabled:
            return

        if data.endswith(b'\r\x00'):
            self.input1.input_done = True
            if self.input1.current_text != b'pencil':
                self.client.dumbterm.print("\r\n\r\n!!!INVALID PASSWORD!!! TERMINATING CONNECTION.\r\n\r\n", end="")
                time.sleep(2)
                self.client.close()
                return
            self.client.dumbterm.print("\r\n\r\nPASSWORD VERIFIED\r\n\r\n", end="")
            time.sleep(2)
            self.client.renderer.set_screen(StudentSearchScreen(self.client))

        elif data == b'\x7f':
            self.input1.current_text = self.input1.current_text[:-1]
        elif not self.input1.input_done and len(data) == 1:
            self.input1.current_text += data
        self.client.renderer.render()

    def __setup_events(self):
        self.client.ch.on("data", self.__handle_input)

class Client:
    def __init__(self, ch) -> None:
        self.ch = ch
        self.dumbterm = ch.dumbterm
        self.renderer = ClientRenderer(ch)

        self.__setup_events()

    def close(self):
        self.dumbterm.clear_screen()
        self.dumbterm.cursor_blink(True)
        self.dumbterm.set_cursor(0, 0)

        self.ch.conn.close()

    def __setup_input_screen(self):
        self.input_screen = LoginScreen(self)
        self.renderer.set_screen(self.input_screen)

    def __setup_events(self):
        self.ch.on("connect", self.__setup_input_screen)