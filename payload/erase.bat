@echo off
chcp 65001
cls
echo Ничего не трогай. Жди...
bin\stm32pr.exe -c port=usb1 -e all > nul
if not %errorlevel% == 0 echo Прошивка НЕ стёрта. Возможно, полётник не в DFU режиме!
if %errorlevel% == 0 echo Операция выполнена - прошивка на полётнике стёрта.
pause > nul