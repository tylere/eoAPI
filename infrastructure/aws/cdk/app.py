"""
CDK Stack definition code for EOAPI
"""
import os
from typing import Any

from aws_cdk import App, CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from config import (
    eoAPISettings,
    eoDBSettings,
    eoRasterSettings,
    eoSTACSettings,
    eoVectorSettings,
)
from constructs import Construct
from eoapi_cdk import (
    PgStacApiLambda,
    PgStacDatabase,
    TiPgApiLambda,
    TitilerPgstacApiLambda,
)

eoapi_settings = eoAPISettings()


class eoAPIconstruct(Stack):
    """Earth Observation API CDK application"""

    def __init__(  # noqa: C901
        self,
        scope: Construct,
        id: str,
        stage: str,
        name: str,
        context_dir: str = "../../",
        **kwargs: Any,
    ) -> None:
        """Define stack."""
        super().__init__(scope, id, **kwargs)

        # vpc = ec2.Vpc(self, f"{id}-vpc", nat_gateways=0)

        vpc = ec2.Vpc(
            self,
            f"{id}-vpc",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="ingress",
                    cidr_mask=24,
                    subnet_type=ec2.SubnetType.PUBLIC,
                ),
                ec2.SubnetConfiguration(
                    name="application",
                    cidr_mask=24,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                ),
                ec2.SubnetConfiguration(
                    name="rds",
                    cidr_mask=28,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                ),
            ],
            nat_gateways=1,
        )
        print(
            """The eoAPI stack use AWS NatGateway for the Raster service so it can reach the internet.
This might incurs some cost (https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat-gateway.html)."""
        )

        interface_endpoints = [
            (
                "SecretsManager Endpoint",
                ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            ),
            (
                "CloudWatch Logs Endpoint",
                ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            ),
        ]
        for (key, service) in interface_endpoints:
            vpc.add_interface_endpoint(key, service=service)

        gateway_endpoints = [("S3", ec2.GatewayVpcEndpointAwsService.S3)]
        for (key, service) in gateway_endpoints:
            vpc.add_gateway_endpoint(key, service=service)

        eodb_settings = eoDBSettings()

        pgstac_db = PgStacDatabase(
            self,
            "pgstac-db",
            vpc=vpc,
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_14
            ),
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            allocated_storage=eodb_settings.allocated_storage,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize(eodb_settings.instance_size),
            ),
            database_name="postgres",
            backup_retention=Duration.days(7),
            deletion_protection=eoapi_settings.stage.lower() == "production",
            removal_policy=RemovalPolicy.SNAPSHOT
            if eoapi_settings.stage.lower() == "production"
            else RemovalPolicy.DESTROY,
            custom_resource_properties={
                "pgstac_version": eodb_settings.pgstac_version,
                "context": eodb_settings.context,
                "mosaic_index": eodb_settings.mosaic_index,
            },
            bootstrapper_lambda_function_options={
                "handler": "handler.handler",
                "runtime": aws_lambda.Runtime.PYTHON_3_10,
                "code": aws_lambda.Code.from_docker_build(
                    path=os.path.abspath(context_dir),
                    file="infrastructure/aws/dockerfiles/Dockerfile.db",
                    build_args={
                        "PYTHON_VERSION": "3.10",
                        "PGSTAC_VERSION": eodb_settings.pgstac_version,
                    },
                    platform="linux/amd64",
                ),
                "timeout": Duration.minutes(5),
                "allow_public_subnet": True,
                "log_retention": logs.RetentionDays.ONE_WEEK,
            },
            pgstac_db_name=eodb_settings.dbname,
            pgstac_username=eodb_settings.user,
            secrets_prefix=os.path.join(stage, name),
        )

        CfnOutput(
            self,
            f"{id}-database-secret-arn",
            value=pgstac_db.pgstac_secret.secret_arn,
            description="Arn of the SecretsManager instance holding the connection info for Postgres DB",
        )

        # eoapi.raster
        if "raster" in eoapi_settings.functions:

            db_secrets = {
                "POSTGRES_HOST": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_DBNAME": pgstac_db.pgstac_secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_USER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": pgstac_db.pgstac_secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_PORT": pgstac_db.pgstac_secret.secret_value_from_json(
                    "port"
                ).to_string(),
            }

            eoraster_settings = eoRasterSettings()
            env = eoraster_settings.env or {}
            if "DB_MAX_CONN_SIZE" not in env:
                env["DB_MAX_CONN_SIZE"] = "1"
            env.update(db_secrets)

            eoraster = TitilerPgstacApiLambda(
                self,
                f"{id}-raster-lambda",
                db=pgstac_db.db,
                db_secret=pgstac_db.pgstac_secret,
                vpc=vpc,
                subnet_selection=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                api_env=env,
                lambda_function_options={
                    "code": aws_lambda.Code.from_docker_build(
                        path=os.path.abspath(context_dir),
                        file="infrastructure/aws/dockerfiles/Dockerfile.raster",
                        build_args={
                            "PYTHON_VERSION": "3.11",
                        },
                        platform="linux/amd64",
                    ),
                    "allow_public_subnet": True,
                    "handler": "handler.handler",
                    "runtime": aws_lambda.Runtime.PYTHON_3_11,
                    "memory_size": eoraster_settings.memory,
                    "timeout": Duration.seconds(eoraster_settings.timeout),
                    "log_retention": logs.RetentionDays.ONE_WEEK,
                },
                buckets=eoraster_settings.buckets,
            )

        # eoapi.stac
        if "stac" in eoapi_settings.functions:
            db_secrets = {
                "POSTGRES_HOST_READER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_HOST_WRITER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_DBNAME": pgstac_db.pgstac_secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_USER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": pgstac_db.pgstac_secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_PORT": pgstac_db.pgstac_secret.secret_value_from_json(
                    "port"
                ).to_string(),
            }

            eostac_settings = eoSTACSettings()
            env = eostac_settings.env or {}
            if "DB_MAX_CONN_SIZE" not in env:
                env["DB_MAX_CONN_SIZE"] = "1"
            if "DB_MIN_CONN_SIZE" not in env:
                env["DB_MIN_CONN_SIZE"] = "1"
            env.update(db_secrets)
            # If raster is deployed we had the TITILER_ENDPOINT env to add the Proxy extension
            if "raster" in eoapi_settings.functions:
                env["TITILER_ENDPOINT"] = eoraster.url.strip("/")

            PgStacApiLambda(
                self,
                id=f"{id}-stac-lambda",
                db=pgstac_db.db,
                db_secret=pgstac_db.pgstac_secret,
                vpc=vpc,
                subnet_selection=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                api_env=env,
                lambda_function_options={
                    "runtime": aws_lambda.Runtime.PYTHON_3_11,
                    "code": aws_lambda.Code.from_docker_build(
                        path=os.path.abspath(context_dir),
                        file="infrastructure/aws/dockerfiles/Dockerfile.stac",
                        build_args={
                            "PYTHON_VERSION": "3.11",
                        },
                        platform="linux/amd64",
                    ),
                    "handler": "handler.handler",
                    "memory_size": eostac_settings.memory,
                    "timeout": Duration.seconds(eostac_settings.timeout),
                    "log_retention": logs.RetentionDays.ONE_WEEK,
                },
            )

        # eoapi.vector
        if "vector" in eoapi_settings.functions:
            db_secrets = {
                "POSTGRES_HOST": pgstac_db.pgstac_secret.secret_value_from_json(
                    "host"
                ).to_string(),
                "POSTGRES_DBNAME": pgstac_db.pgstac_secret.secret_value_from_json(
                    "dbname"
                ).to_string(),
                "POSTGRES_USER": pgstac_db.pgstac_secret.secret_value_from_json(
                    "username"
                ).to_string(),
                "POSTGRES_PASS": pgstac_db.pgstac_secret.secret_value_from_json(
                    "password"
                ).to_string(),
                "POSTGRES_PORT": pgstac_db.pgstac_secret.secret_value_from_json(
                    "port"
                ).to_string(),
            }

            eovector_settings = eoVectorSettings()
            env = eovector_settings.env or {}

            if "DB_MAX_CONN_SIZE" not in env:
                env["DB_MAX_CONN_SIZE"] = "1"
            if "DB_MIN_CONN_SIZE" not in env:
                env["DB_MIN_CONN_SIZE"] = "1"

            env.update(db_secrets)

            TiPgApiLambda(
                self,
                f"{id}-vector-lambda",
                vpc=vpc,
                db=pgstac_db.db,
                db_secret=pgstac_db.pgstac_secret,
                subnet_selection=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                api_env=env,
                lambda_function_options={
                    "runtime": aws_lambda.Runtime.PYTHON_3_11,
                    "code": aws_lambda.Code.from_docker_build(
                        path=os.path.abspath(context_dir),
                        file="infrastructure/aws/dockerfiles/Dockerfile.vector",
                        build_args={
                            "PYTHON_VERSION": "3.11",
                        },
                        platform="linux/amd64",
                    ),
                    "handler": "handler.handler",
                    "memory_size": eovector_settings.memory,
                    "timeout": Duration.seconds(eovector_settings.timeout),
                    "log_retention": logs.RetentionDays.ONE_WEEK,
                },
            )


app = App()


eoapi_stack = eoAPIconstruct(
    app,
    f"{eoapi_settings.name}-{eoapi_settings.stage}",
    eoapi_settings.name,
    eoapi_settings.stage,
    env={
        "account": os.environ["CDK_DEFAULT_ACCOUNT"],
        "region": os.environ["CDK_DEFAULT_REGION"],
    },
)

# Tag infrastructure
for key, value in {
    "Project": eoapi_settings.name,
    "Stack": eoapi_settings.stage,
    "Owner": eoapi_settings.owner,
    "Client": eoapi_settings.client,
}.items():
    if value:
        Tags.of(eoapi_stack).add(key, value)


app.synth()
