from .config import ENABLED


if ENABLED:
    from .main import main

    main()
else:
    print("gfs_15min_guardian disabled; set GFS15M_ENABLED=1 to enable automatic CSV conversion")
