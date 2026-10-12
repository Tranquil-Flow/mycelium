@echo off
rem Build the A9 int8 kernel DLL and its self test with MSVC.
rem Run from an "x64 Native Tools Command Prompt for VS" (cl.exe on PATH).
rem Output: out\mycelium_int8.dll, out\selftest.exe; prints SELFTEST PASS on success.
rem /Brepro (compiler and linker): no build timestamp in the PE image, so the same source and
rem toolchain give the same DLL bytes; the DLL sha256 is the provider build identity.
setlocal
cd /d "%~dp0"
if not exist out mkdir out
cl /nologo /Brepro /O2 /arch:AVX2 /openmp /fp:precise /W4 /LD mycelium_int8.c /Feout\mycelium_int8.dll /Foout\ /link /Brepro || exit /b 1
cl /nologo /Brepro /O2 /arch:AVX2 /openmp /fp:precise /W4 selftest.c mycelium_int8.c /Feout\selftest.exe /Foout\ /link /Brepro || exit /b 1
out\selftest.exe || exit /b 1
certutil -hashfile out\mycelium_int8.dll SHA256
endlocal
