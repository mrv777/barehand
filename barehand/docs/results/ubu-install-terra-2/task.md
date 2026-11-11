There is a blank aarch64 (ARM64) UEFI virtual machine attached to this session. You can see its screen and drive its keyboard and mouse with the computer_use tool, and the barehand_machine tool controls the machine itself. Its hard disk is empty and an Ubuntu Server 26.04 live installer CD is in its drive. The machine has working network access through DHCP.

Please install Ubuntu Server onto the machine's disk using the installer. Use the whole disk with the default storage layout, give it the hostname `barehand`, and create a user `lab` with password `vm-throwaway-1` (this is a throwaway lab machine). No extra packages or snaps are needed, and you do not need to install the OpenSSH server. Then reboot it and prove that it now boots from the installed disk and not from the CD.

When you are done, tell me plainly what you did and what you saw on the screen that shows it worked.
