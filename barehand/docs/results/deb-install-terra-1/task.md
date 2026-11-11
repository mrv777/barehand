There is a blank aarch64 (ARM64) UEFI virtual machine attached to this session. You can see its screen and drive its keyboard and mouse with the computer_use tool, and the barehand_machine tool controls the machine itself. Its hard disk is empty and a Debian 13 network-installer CD is in its drive. The machine has working network access through DHCP.

Please install Debian onto the machine's disk using the text-mode installer. Use the whole disk with the default partitioning, give it the hostname `barehand`, set the root password to `vm-throwaway-1`, and create a user `lab` with password `vm-throwaway-1` (this is a throwaway lab machine). A minimal system is enough: no desktop environment, only the standard system utilities. Then reboot it and prove that it now boots from the installed disk and not from the CD.

When you are done, tell me plainly what you did and what you saw on the screen that shows it worked.
