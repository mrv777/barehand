There is a small ARM64 virtual machine attached to this session. It runs Alpine Linux from its hard disk, you can see its screen and drive its keyboard with the computer_use tool, and the barehand_machine tool controls the machine itself. It is a throwaway lab machine and the root password is `vm-throwaway-1`.

When this machine is switched on it never gets into Linux: it ends up at a firmware prompt and stays there. The disk is fine. Please make the machine start Linux from its disk by itself, as a lasting setting made with the machine's own firmware menus (not only for one boot), so that a plain restart with nobody touching the keyboard ends at the Linux login prompt. Restart it once at the end to prove that (barehand_machine reset is allowed).

When you are done, tell me plainly what was wrong, what you changed, whether it is a one-time or a lasting change, and what you saw on the screen that shows it worked.
