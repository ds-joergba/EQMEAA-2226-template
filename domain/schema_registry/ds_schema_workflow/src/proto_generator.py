import logging
import os
import shutil
from pathlib import Path
from typing import Tuple

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.timestamp_pb2 import Timestamp


class ProtoGenerator:
    def __init__(self):
        self.serialized_proto = None
        self.temp_path = os.path.join(Path(os.getcwd()).absolute().as_posix(), "temp")
        self.full_package_name = None

    def create_serialized_proto(self, proto_file: str) -> Tuple[str, bytes]:
        """
        Compile the .proto file to a descriptor set and dynamically load it.

        :param proto_file: Relative or absolute path to the proto file.
        :return: Tuple of (package name, serialized proto)
        """
        try:
            file_name = os.path.basename(proto_file)
            schema_dir = os.path.dirname(proto_file)
            os.makedirs(self.temp_path, exist_ok=True)
            descriptor_path = os.path.join(self.temp_path, f"{file_name}.desc")

            cmd = (
                "python -m grpc_tools.protoc "
                f"--proto_path={schema_dir} "
                f"--descriptor_set_out={descriptor_path} "
                f"--include_imports {file_name}"
            )
            os_exception = os.system(cmd)

            if os_exception != 0:
                logging.error("Protoc execution failed!")
                raise ValueError

            with open(descriptor_path, "rb") as f:
                descriptor_set = descriptor_pb2.FileDescriptorSet()
                descriptor_set.ParseFromString(f.read())

            pool = descriptor_pool.DescriptorPool()
            for fd_proto in descriptor_set.file:
                pool.Add(fd_proto)

            factory = message_factory.MessageFactory(pool)
            message_classes = {}
            for fd_proto in descriptor_set.file:
                for message_type in fd_proto.message_type:
                    full_name = f"{fd_proto.package}.{message_type.name}"
                    descriptor = pool.FindMessageTypeByName(full_name)
                    message_classes[full_name] = factory.GetPrototype(descriptor)

            shutil.rmtree(self.temp_path)

            # descriptor set contains also dependencies, but we are only interested in the custom one
            fd_proto = next(fd for fd in descriptor_set.file if fd.name.endswith(os.path.basename(proto_file)))
            main_message_name = fd_proto.message_type[0].name if fd_proto.message_type else ""
            self.full_package_name = f"{fd_proto.package}.{main_message_name}"
            self.serialized_proto = fd_proto.SerializeToString()

            return self.full_package_name, self.serialized_proto

        except Exception as error:
            if os.path.exists(self.temp_path):
                logging.error(f"Error occurred. Removing temp dir:{self.temp_path}")
                shutil.rmtree(self.temp_path)
            raise error

    def check_descriptor(self):
        """Proves that dataflow will later be able to load the descriptor during runtime."""
        try:
            pool = descriptor_pool.DescriptorPool()

            file_descriptor = descriptor_pb2.FileDescriptorProto.FromString(self.serialized_proto)

            # first the depencies need to be added to the pool
            for dep in file_descriptor.dependency:
                if "timestamp.proto" in dep:
                    pool.Add(descriptor_pb2.FileDescriptorProto.FromString(Timestamp.DESCRIPTOR.file.serialized_pb))
                else:
                    logging.warning(f"Dependency {dep} is being ignored!")
            pool.Add(file_descriptor)

            factory = message_factory.MessageFactory(pool)

            schema_class = factory.GetPrototype(pool.FindMessageTypeByName(self.full_package_name))
            schema_class_instance = schema_class()
            class_name = schema_class_instance.DESCRIPTOR.name
            print(f"Successfully created python class from the descriptor of {class_name}")

        except Exception as e:
            logging.error(
                f"Creating python class from proto descriptor {self.serialized_proto} "
                f"of schema {self.full_package_name} failed."
            )
            raise e
