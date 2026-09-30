"""Entry point for the frozen Companion executable (aicutout-companion.exe)."""
import multiprocessing

from plugin.UI.companion import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
