# Wyrm Object Extensions

## Introduction

Wyrm provides critical generic services for programs:

  - A macro scripting language for writing extensions
  - An object model suitable for retained mode GUI _or_ resource tracking
    within a GUI toolkit.
  - A interoperability layer for multi language and platform projects.

Key goals:

  - Extreme interop with multiple programming languages. Wyrm should have
    first-class support for: C++, Python, Zig, Go.
  - Minimal memory requirements; suitable for embedding in deeply embedded
    projects or desktop.
  - Facility for compiling scripts into code.

## Inspiration and Competition

Major Projects providing similar services:

  - Qt: QObject, moc, and signal/slot design demonstrated by both the
    QWidget interface and QML interface.
  - GLib: the underlying GObject model and event subsystem
  - Lua: a scripting language with very similar goals
  - elisp: Emacs architecture and design

Historic Inspiration

  - [Hypercard](https://hypercard.org/) - largely because of.. 
  - [Dylan](https://opendylan.org/) – Functional programming with easier syntax

## Data Model

### Primitive Types

A wyrm value consists of an enumerated type tag and a register value. The
register value should align to a single machine register on most
architectures. A 'value' structure combines these for a full definition.

The following primitive values are represented by wyrm. These generally
correspond to atoms and native collections:

  - word: machine word
  - uword: unsigned machine word
  - coord_int: half-word pair (x,y)
  - float: 32bit float

Types requiring additional memory or resource management must inherit
from the Wyrm object type. The low level virtual machine understand
a handful of object types. All objects use a pointer in the primitive
to the actual object data. Each type inherits from object:

  - str: string data
  - table: core table type, the primary object type for scripts
  - fiber: execution task
  - type: type information
