@echo off
chcp 65001
cls
echo Ничего не трогай. Жди...
if not exist fw.bin goto notfw
bin\stm32pr.exe -c port=usb1 -w fw.bin 0x08000000 -v > nul
if not %errorlevel% == 0 echo Прошивка НЕ записана. Возможно, полётник не в DFU режиме!
if %errorlevel% == 0 echo Операция выполнена - прошивка 'fw.bin' записана на полётник.
pause > nul
goto end
:notfw
echo Прошивка НЕ записана. Файл 'fw.bin' не найден в папке программы!
pause > nul
:end