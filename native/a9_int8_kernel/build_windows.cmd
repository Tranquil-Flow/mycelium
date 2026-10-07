@echo off
rem Build the A9 int8 kernel DLL and its self test with MSVC.
rem Run from an "x64 Native Tools Command Prompt for VS" (cl.exe on PATH).
rem Output: out\mycelium_int8.dll, out\selftest.exe; prints SELFTEST PASS on success.
setlocal
cd /d "%~dp0"
if not exist out mkdir out
cl /nologo /O2 /arch:AVX2 /openmp /fp:precise /W4 /LD mycelium_int8.c /Feout\mycelium_int8.dll /Foout\ || exit /b 1
cl /nologo /O2 /arch:AVX2 /openmp /fp:precise /W4 selftest.c mycelium_int8.c /Feout\selftest.exe /Foout\ || exit /b 1
out\selftest.exe || exit /b 1
certutil -hashfile out\mycelium_int8.dll SHA256
endlocal
