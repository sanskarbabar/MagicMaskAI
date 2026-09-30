# Source this: puts the user-scope llvm-mingw toolchain and CMake on PATH (Git Bash).
LAD="$(cygpath -u "$LOCALAPPDATA")"
LLVM="$LAD/Microsoft/WinGet/Packages/MartinStorsjo.LLVM-MinGW.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/llvm-mingw-20260616-ucrt-x86_64/bin"
export PATH="$LLVM:/c/Program Files/CMake/bin:$PATH"
