import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from noise_remover.cli import main
        main(sys.argv[2:])
    else:
        from noise_remover.gui import main
        main()
