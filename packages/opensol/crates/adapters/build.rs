fn main() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(feature = "envd")]
    {
        let mut config = prost_build::Config::new();
        config.protoc_executable(protoc_bin_vendored::protoc_bin_path()?);
        config.compile_protos(
            &["protocols/envd/process.proto"],
            &[
                std::path::PathBuf::from("protocols/envd"),
                protoc_bin_vendored::include_path()?,
            ],
        )?;
    }
    #[cfg(feature = "sandboxd")]
    {
        let mut config = tonic_prost_build::Config::new();
        config.protoc_executable(protoc_bin_vendored::protoc_bin_path()?);
        tonic_prost_build::configure()
            .build_server(cfg!(feature = "protocol-server"))
            .generate_default_stubs(true)
            .compile_with_config(
                config,
                &[std::path::PathBuf::from("protocols/sandboxd/process.proto")],
                &[
                    std::path::PathBuf::from("protocols/sandboxd"),
                    protoc_bin_vendored::include_path()?,
                ],
            )?;
    }
    #[cfg(feature = "openshell")]
    {
        let mut config = tonic_prost_build::Config::new();
        config.protoc_executable(protoc_bin_vendored::protoc_bin_path()?);
        tonic_prost_build::configure()
            .build_server(cfg!(feature = "protocol-server"))
            .generate_default_stubs(true)
            .compile_with_config(
                config,
                &[std::path::PathBuf::from(
                    "protocols/openshell/openshell.proto",
                )],
                &[
                    std::path::PathBuf::from("protocols/openshell"),
                    protoc_bin_vendored::include_path()?,
                ],
            )?;
    }
    println!("cargo:rerun-if-changed=protocols");
    Ok(())
}
