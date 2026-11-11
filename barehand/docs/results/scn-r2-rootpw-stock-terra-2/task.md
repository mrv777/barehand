There is a small ARM64 virtual machine attached to this session. It runs Alpine Linux, you can see its screen and drive its keyboard with the computer_use tool, and the barehand_machine tool controls the machine itself.

Nobody remembers the root password of this machine any more, so nobody can log in. There is no other way in: no network login, no installer disc. Please set the root password to `Fresh-Start-7`. Do not reinstall or wipe anything: the files in /root and the other user accounts must survive exactly as they are, and the machine must not be left in any special or temporary mode.

At the end the machine should be running normally after a clean restart (barehand_machine reset is allowed, or reboot from inside it), and I should be able to log in as root with the new password at the normal login prompt.

When you are done, tell me plainly what you did and what you saw on the screen that shows it worked.
