# --------------------------------------------------------------------------------
# Copyright (c) 2023 John Woo
#
# TelServ (TelnetServer)
# 
# This file is part of the TelServ project. For full license information, see the
# LICENSE.md file at the root of the source code directory
# --------------------------------------------------------------------------------


import telserv

def main():
    server = telserv.TelnetServer(
        host="192.168.254.5",
        port=23,
    )

    server.run()

if __name__ == "__main__":
    main()