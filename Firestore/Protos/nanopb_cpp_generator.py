#!/usr/bin/env python

# Copyright 2018 Google
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generates and massages protocol buffer outputs.
"""

from __future__ import print_function

import sys

import io
import nanopb_generator as nanopb
import os
import os.path
import shlex

from google.protobuf.descriptor_pb2 import FieldDescriptorProto

if sys.platform == 'win32':
  import msvcrt  # pylint: disable=g-import-not-at-top

# The plugin_pb2 package loads descriptors on import, but doesn't defend
# against multiple imports. Reuse the plugin package as loaded by the
# nanopb_generator.
plugin_pb2 = nanopb.plugin_pb2
nanopb_pb2 = nanopb.nanopb_pb2


def main():
  # Parse request
  if sys.platform == 'win32':
    # Set stdin and stdout to binary mode
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

  data = io.open(sys.stdin.fileno(), 'rb').read()
  request = plugin_pb2.CodeGeneratorRequest.FromString(data)

  # Preprocess inputs, changing types and nanopb defaults
  use_anonymous_oneof(request)
  use_bytes_for_strings(request)
  use_malloc(request)

  # Generate code
  options = nanopb_parse_options(request)
  pretty_printing_info = {};
  parsed_files = nanopb_parse_files(request, options, pretty_printing_info)
  #raise Exception(pretty_printing_info)
  results = nanopb_generate(request, options, parsed_files)
  response = nanopb_write(results, pretty_printing_info)

  # Write to stdout
  io.open(sys.stdout.fileno(), 'wb').write(response.SerializeToString())


def use_malloc(request):
  """Mark all variable length items as requiring malloc.

  By default nanopb renders string, bytes, and repeated fields (dynamic fields)
  as having the C type pb_callback_t. Unfortunately this type is incompatible
  with nanopb's union support.

  The function performs the equivalent of adding the following annotation to
  each dynamic field in all the protos.

    string name = 1 [(nanopb).type = FT_POINTER];

  Args:
    request: A CodeGeneratorRequest from protoc. The descriptors are modified
      in place.
  """
  dynamic_types = [
      FieldDescriptorProto.TYPE_STRING,
      FieldDescriptorProto.TYPE_BYTES,
  ]

  for _, message_type in iterate_messages(request):
    for field in message_type.field:
      dynamic_type = field.type in dynamic_types
      repeated = field.label == FieldDescriptorProto.LABEL_REPEATED

      if dynamic_type or repeated:
        ext = field.options.Extensions[nanopb_pb2.nanopb]
        ext.type = nanopb_pb2.FT_POINTER


def prepare_pretty_printing_support(request):
  """FIXME"""
  fields_by_class_and_file = {}
  for fdesc in request.proto_file:
    short_filename = fdesc.name.replace('.proto', '')
    fields_by_class_and_file[short_filename] = {}

    # all_extensions = []
    # ctr = 0
    # for classname, extension in nanopb.iterate_extensions(fdesc):
    #   all_extensions.append(extension)
    #   ctr += 1
    #   if ctr == 1:
    #     raise Exception(extension)

    #raise Exception(all_extensions)

    for classname, message_type in nanopb.iterate_messages(fdesc):
      # if str(classname) == 'ListDocumentsRequest':
      #   raise Exception(message_type)
      # if str(classname) == 'TargetChange':
      #   raise Exception(message_type)
      full_classname = fdesc.package.replace('.', '_') + '_' + str(classname)
      fields_by_class_and_file[short_filename][full_classname] = {}
      fields_by_class_and_file[short_filename][full_classname]['short_classname'] = classname
      fields_by_class_and_file[short_filename][full_classname]['fields'] = []
      for field in message_type.field:
        # if field.name == 'cause':
          #nanopb_opt = nanopb.get_nanopb_suboptions(fdesc, message_type, field.name)
          # raise Exception(nanopb_opt)

        # if field.name == 'cause':
        #   raise Exception(field.options)
        fields_by_class_and_file[short_filename][full_classname]['fields'].append(field)

  return fields_by_class_and_file

def use_anonymous_oneof(request):
  """Use anonymous unions for oneofs if they're the only one in a message.

  Equivalent to setting this option on messages where it applies:

    option (nanopb).anonymous_oneof = true;

  Args:
    request: A CodeGeneratorRequest from protoc. The descriptors are modified
      in place.
  """
  for _, message_type in iterate_messages(request):
    if len(message_type.oneof_decl) == 1:
      ext = message_type.options.Extensions[nanopb_pb2.nanopb_msgopt]
      ext.anonymous_oneof = True


def use_bytes_for_strings(request):
  """Always use the bytes type instead of string.

  By default, nanopb renders proto strings as having the C type char* and does
  not include a separate size field, getting the length of the string via
  strlen(). Unfortunately this prevents using strings with embedded nulls,
  which is something the wire format supports.

  Fortunately, string and bytes proto fields are identical on the wire and
  nanopb's bytes representation does have an explicit length, so this function
  changes the types of all string fields to bytes. The generated code will now
  contain pb_bytes_array_t.

  There's no nanopb or proto option to control this behavior. The equivalent
  would be to hand edit all the .proto files :-(.

  Args:
    request: A CodeGeneratorRequest from protoc. The descriptors are modified
      in place.
  """
  for names, message_type in iterate_messages(request):
    for field in message_type.field:
      if field.type == FieldDescriptorProto.TYPE_STRING:
        field.type = FieldDescriptorProto.TYPE_BYTES


def iterate_messages(request):
  """Iterates over all messages in all files in the request.

  Args:
    request: A CodeGeneratorRequest passed by protoc.

  Yields:
    names: a nanopb.Names object giving a qualified name for the message
    message_type: a DescriptorProto for the message.
  """
  for fdesc in request.proto_file:
    for names, message_type in nanopb.iterate_messages(fdesc):
      yield names, message_type


def nanopb_parse_options(request):
  """Parses nanopb_generator command-line options from the given request.

  Args:
    request: A CodeGeneratorRequest passed by protoc.

  Returns:
    Nanopb's options object, obtained via optparser.
  """
  # Parse options the same as nanopb_generator.main_plugin() does.
  args = shlex.split(request.parameter)
  options, _ = nanopb.optparser.parse_args(args)

  # Force certain options
  options.extension = '.nanopb'

  # Replicate options setup from nanopb_generator.main_plugin.
  nanopb.Globals.verbose_options = options.verbose

  # Google's protoc does not currently indicate the full path of proto files.
  # Instead always add the main file path to the search dirs, that works for
  # the common case.
  options.options_path.append(os.path.dirname(request.file_to_generate[0]))

  return options


def nanopb_parse_files(request, options, pretty_printing_info):
  """Parses the files in the given request into nanopb ProtoFile objects.

  Args:
    request: A CodeGeneratorRequest, as passed by protoc.
    options: The command-line options from nanopb_parse_options.

  Returns:
    A dictionary of filename to nanopb.ProtoFile objects, each one representing
    the parsed form of a FileDescriptor in the request.
  """
  # Process any include files first, in order to have them available as
  # dependencies
  parsed_files = {}
  for fdesc in request.proto_file:
    parsed_file = nanopb.parse_file(fdesc.name, fdesc, options)
    parsed_files[fdesc.name] = parsed_file

    base_filename = fdesc.name.replace('.proto', '')
    pretty_printing_info[base_filename] = []
    for m in parsed_file.messages:
      pretty_printing_info[base_filename].append(MessagePrettyPrintingInfo(m))

  return parsed_files


class OneOfPrettyPrintingInfo:
  def __init__(self, field, message):
    raise Exception('Not a oneof')


class FieldPrettyPrintingInfo:
  def __init__(self, field, message):
    self.name = field.name

    self.is_optional = field.rules == 'OPTIONAL' and field.allocation == 'STATIC'
    self.is_repeated = field.rules == 'REPEATED'

    try:
      self.oneof = OneOfPrettyPrintingInfo(field, message)
    except:
      self.oneof = None

  def __repr__(self):
    return 'FieldPrettyPrintingInfo{name: %s, is_optional: %s, is_repeated: %s, oneof: %s}' % (self.name, self.is_optional, self.is_repeated, self.oneof)


class MessagePrettyPrintingInfo:
  def __init__(self, message):
    self.short_classname = message.name.parts[-1]
    self.full_classname = str(message.name)
    self.fields = [FieldPrettyPrintingInfo(f, message) for f in message.fields]


  def __repr__(self):
    return 'MessagePrettyPrintingInfo{short_classname: %s, full_classname: %s, fields: %s}' % (self.short_classname, self.full_classname, self.fields)


def nanopb_generate(request, options, parsed_files):
  """Generates C sources from the given parsed files.

  Args:
    request: A CodeGeneratorRequest, as passed by protoc.
    options: The command-line options from nanopb_parse_options.
    parsed_files: A dictionary of filename to nanopb.ProtoFile, as returned by
      nanopb_parse_files().

  Returns:
    A list of nanopb output dictionaries, each one representing the code
    generation result for each file to generate. The output dictionaries have
    the following form:

        {
          'headername': Name of header file, ending in .h,
          'headerdata': Contents of the header file,
          'sourcename': Name of the source code file, ending in .c,
          'sourcedata': Contents of the source code file
        }
  """
  output = []

  for filename in request.file_to_generate:
    for fdesc in request.proto_file:
      if fdesc.name == filename:
        results = nanopb.process_file(filename, fdesc, options, parsed_files)
        output.append(results)

  return output


def nanopb_write(results, pretty_printing_info):
  """Translates nanopb output dictionaries to a CodeGeneratorResponse.

  Args:
    results: A list of generated source dictionaries, as returned by
      nanopb_generate().

  Returns:
    A CodeGeneratorResponse describing the result of the code generation
    process to protoc.
  """
  response = plugin_pb2.CodeGeneratorResponse()

  for result in results:
    f = response.file.add()
    f.name = result['headername']
    f.content = result['headerdata']

    f = response.file.add()
    f.name = result['headername']
    f.insertion_point = 'includes'
    f.content = '''#include "absl/strings/str_cat.h"
#include "nanopb_pretty_printers.h"

namespace firebase {
namespace firestore {'''

    f = response.file.add()
    f.name = result['headername']
    f.insertion_point = 'eof'
    f.content = '''
}  // namespace firestore
}  // namespace firebase'''

    base_filename = f.name.replace('.nanopb.h', '')

    for p in pretty_printing_info[base_filename]:
      f = response.file.add()
      f.name = result['headername']
      f.insertion_point = 'struct:' + p.full_classname

      f.content = '''
    std::string ToString(int indent = 0) const {
        std::string result{"%s("};\n\n''' % (p.short_classname)

      for field in p.fields:
        f.content += ' ' * 8 + add_printing_for_field(field) + '\n'

      f.content += '''
        result += ')';
        return result;
    }'''

    f = response.file.add()
    f.name = result['sourcename']
    f.content = result['sourcedata']

    f = response.file.add()
    f.name = result['sourcename']
    f.insertion_point = 'includes'
    f.content = '''namespace firebase {
namespace firestore {'''

    f = response.file.add()
    f.name = result['sourcename']
    f.insertion_point = 'eof'
    f.content = '''
}  // namespace firestore
}  // namespace firebase'''


  return response


def add_printing_for_field(field):
  if field.is_optional:
    return add_printing_for_optional(field)
  elif field.is_repeated:
    return add_printing_for_repeated(field)
  elif field.oneof:
    return add_printing_for_oneof(field)
  else:
    return add_printing_for_singular(field)


def add_printing_for_oneof(field, oneof):
  return 'if (which_%s == ???) ' % (oneof.name) + add_printing_for_singular(field)


def add_printing_for_repeated(field):
  name = field.name
  count = name + '_count'
  return '''if (%s) result += absl::StrCat("%s: ",
            ToStringImpl(%s, %s, indent + 1), "\\n");''' % (count, name, name, count)


def add_printing_for_optional(field):
  return 'if (has_%s) ' % (field.name) + add_printing_for_singular(field)


def add_printing_for_singular(field):
  name = field.name
  return '''result += absl::StrCat("%s: ",
            ToStringImpl(%s, indent), "\\n");''' % (name, name)


if __name__ == '__main__':
  main()
