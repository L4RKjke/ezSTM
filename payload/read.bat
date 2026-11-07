@echo off
chcp 65001
cls
del /F /Q fw.bin > nul
cls
echo Ничего не трогай. Жди...
bin\stm32pr.exe -c port=usb1 -r 0x08000000 0x80000 fw.bin > nul
bin\stm32pr.exe -c port=usb1 -r 0x08000000 0x100000 fw.bin > nul
if not exist fw.bin echo Прошивка НЕ сохранена. Возможно, полётник не в DFU режиме!
if exist fw.bin echo Операция выполнена - прошивка 'fw.bin' находится в папке программы.
pause > nul