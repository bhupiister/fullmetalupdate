#! /bin/sh

# original
#echo "0" > /sys/class/backlight/buzzer_backlight/brightness
#/usr/bin/python3 /usr/fullmetalupdate/fullmetalupdate.py --config /etc/fullmetalupdate/rauc_hawkbit/config.cfg

# dev mode
python3 ../fullmetalupdate.py --config ./config.cfg
